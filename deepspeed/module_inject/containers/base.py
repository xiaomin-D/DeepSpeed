# Create a container object to save model-specific tensors using the policy file above.
from abc import ABC
import torch


class BaseConvolutionContainer(ABC):
    # not implemented
    def __init__(self):
        pass


class BaseTransformerContainer(ABC):
    def __init__(self, policy):
        self.policy = policy

        self.megatron_v2 = self.policy.is_megatron_v2

        # configuration for models. todo: can this be moved to a pydantic model config?
        self.hidden_size = None
        self.num_attention_heads = None
        self.mp_size = 1
        self.fp16 = False
        self.is_meta = False

        # Attention tensors
        self.qkvw = None
        self.qkvb = None
        self.dense_w = None
        self.dense_b = None
        # MLP tensors
        self._h4h_w = None
        self._h4h_b = None
        self._4hh_w = None
        self._4hh_b = None
        # LayerNorm tensors
        self.attn_nw = None
        self.attn_nb = None
        self.input_nw = None
        self.input_nb = None

    def initialize_tensors(self):
        # todo: refactor this to become part of config instead of tensor list
        self.set_hidden_heads(*self.policy.get_hidden_heads())
        assert self.num_attention_heads % self.mp_size == 0,\
                "To run the model parallel across the GPUs, the attention_heads require to be divisible by the world_size!" +\
                "This is because the attention computation is partitioned evenly among the parallel GPUs."

        # Set the tensors from policy (user module) to container (DS module)
        self.set_attention(*self.policy.attention())
        self.set_mlp(*self.policy.mlp())
        self.set_layernorm(*self.policy.layernorm())

        for k, v in self.__dict__.items():
            if isinstance(v, torch.Tensor) or isinstance(v, torch.nn.Parameter):
                print(f"{k}.is_meta = {self.__dict__[k].is_meta}")

        # Set is_meta flag
        self.is_meta = self.qkvw.is_meta

    def convert_to_required_dtype(self, dtype):
        # Note: converting tensors to fp16 requires that we do it in-place using self.__dict__ and not make a list/dict copy
        if dtype == torch.half:
            for k, v in self.__dict__.items():
                if isinstance(v, torch.Tensor) or isinstance(v, torch.nn.Parameter):
                    self.__dict__[k] = v.half()

    def set_dtype(self, fp16=False):
        self.fp16 = fp16

    # TODO (lekurile): Move this to MoE container later
    def set_moe(self, moe=False):
        self.moe = moe

    def set_tensor_parallel_config(self, mp_size, mp_group):
        self.mp_size = mp_size
        self.mp_group = mp_group

    def set_quantization_config(self, quantize, quantizer):
        self.quantize = quantize
        self.quantizer = quantizer

    def set_hidden_heads(self, hidden_size, num_attention_heads):
        self.hidden_size = hidden_size
        self.num_attention_heads = num_attention_heads

    def set_attention(self, qkvw, qkvb, dense_w, dense_b):
        self.qkvw = qkvw
        self.qkvb = qkvb
        self.dense_w = dense_w
        self.dense_b = dense_b

    def set_mlp(self, _h4h_w, _h4h_b, _4hh_w, _4hh_b):
        self._h4h_w = _h4h_w
        self._h4h_b = _h4h_b
        self._4hh_w = _4hh_w
        self._4hh_b = _4hh_b

    def set_layernorm(self, attn_nw, attn_nb, input_nw, input_nb):
        self.attn_nw = attn_nw
        self.attn_nb = attn_nb
        self.input_nw = input_nw
        self.input_nb = input_nb

    def apply_weight_quantization(self):
        # quantize attention weights
        self.module.attention.attn_qkvw = self.quantizer.quantize(
            self.module.attention.attn_qkvw)
        self.module.attention.attn_ow = self.quantizer.quantize(
            self.module.attention.attn_ow)

        # quantize mlp weights
        self.module.mlp.inter_w = self.quantizer.quantize(self.module.mlp.inter_w)
        self.module.mlp.output_w = self.quantizer.quantize(self.module.mlp.output_w)

    def apply_tensor_parallelism(self, mp_replace):
        # todo: Ask Reza if there is a fixed strategy for this copying and if possible without mp_replace when mp_size=1
        if self.is_meta:
            if self.qkvb is None:
                self.module.attention.attn_qkvb = None
            if self.dense_b is None:
                self.module.attention.attn_ob = None
            pass
        else:
            # setup the new Attention module
            self.module.attention.attn_qkvw = mp_replace.qkv_copy(
                self.module.attention.attn_qkvw,
                self.qkvw)
            self.module.attention.attn_qkvb = mp_replace.qkv_copy(
                self.module.attention.attn_qkvb,
                self.qkvb)
            self.module.attention.attn_ow = mp_replace.copy(
                self.module.attention.attn_ow,
                self.dense_w)
            self.module.attention.attn_ob = mp_replace.copy(
                self.module.attention.attn_ob,
                self.dense_b)

            # setup the new MLP module
            self.module.mlp.inter_w = mp_replace.copy(self.module.mlp.inter_w,
                                                      self._h4h_w)
            self.module.mlp.inter_b = mp_replace.copy(self.module.mlp.inter_b,
                                                      self._h4h_b)
            self.module.mlp.output_w = mp_replace.copy(self.module.mlp.output_w,
                                                       self._4hh_w)
            self.module.mlp.output_b = mp_replace.copy(self.module.mlp.output_b,
                                                       self._4hh_b)

            # Apply weight quantization
            self.apply_weight_quantization()

    def copy_data_to_new_module(self):
        if self.attn_nw is None:
            self.module.mlp.attn_nw = self.attn_nw
            self.module.mlp.attn_nb = self.attn_nb
        else:
            if self.is_meta:
                pass
            else:
                self.module.mlp.attn_nw.data.copy_(
                    self.attn_nw.to(torch.cuda.current_device()))
                self.module.mlp.attn_nb.data.copy_(
                    self.attn_nb.to(torch.cuda.current_device()))

        if self.is_meta:
            pass
        else:
            self.module.norm_w.data.copy_(self.input_nw.to(torch.cuda.current_device()))
            self.module.norm_b.data.copy_(self.input_nb.to(torch.cuda.current_device()))

    def transpose(self):
        if self.is_meta:
            pass
        else:
            if self.attn_linear_layer:
                self.qkvw = self.transpose_impl(self.qkvw.data)
                self.dense_w = self.transpose_impl(self.dense_w.data)

            if self.mlp_linear_layer:
                # TODO (lekurile): Figure out if not self.moe need to be here
                #if not self.moe and self.is_meta:
                self._h4h_w = self.transpose_impl(self._h4h_w.data)
                self._4hh_w = self.transpose_impl(self._4hh_w.data)

            self.megatron_transpose()

    # TODO (lekurile): Implement function in megatron "feature" container
    def megatron_transpose(self):
        if self.megatron_v2:
            self.qkvw = torch.nn.parameter.Parameter(
                self.transpose_qkv_alignment(self.qkvw).contiguous())
            self.qkvb = torch.nn.parameter.Parameter(
                self.transpose_qkv_alignment(self.qkvb).contiguous())

    def transpose_impl(self, data):
        data = data.contiguous()
        data.reshape(-1).copy_(data.transpose(-1, -2).contiguous().reshape(-1))
        data = data.reshape(data.shape[-1], data.shape[-2])
        data.to(torch.cuda.current_device())
        return data

    # TODO (lekurile): This function should be moved to megatron "feature" container
    def transpose_qkv_alignment(self, x):
        attention_head_size = x.shape[-1] // self.num_attention_heads
        new_x_shape = x.size()[:-1] + (self.num_attention_heads, attention_head_size)
        x_1 = x.view(*new_x_shape)
        (q, k, v) = torch.split(x_1, (x_1.shape[-1] // 3), dim=(x_1.dim() - 1))
        if len(q.shape) > 2:
            return torch.cat((q.reshape(q.shape[0],
                                        -1),
                              k.reshape(q.shape[0],
                                        -1),
                              v.reshape(q.shape[0],
                                        -1)),
                             dim=-1).reshape(x.shape)
        else:
            return torch.cat((q.reshape(-1),
                              k.reshape(-1),
                              v.reshape(-1)),
                             dim=-1).reshape(x.shape)