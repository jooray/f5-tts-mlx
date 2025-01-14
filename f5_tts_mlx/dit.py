"""
ein notation:
b - batch
n - sequence
nt - text sequence
nw - raw wave length
d - dimension
"""

from __future__ import annotations

import mlx.core as mx
import mlx.nn as nn

from einops.array_api import repeat

from f5_tts_mlx.modules import (
    Attention,
    FeedForward,
    RotaryEmbedding,
    TimestepEmbedding,
    ConvNeXtV2Block,
    ConvPositionEmbedding,
    precompute_freqs_cis,
    get_pos_embed_indices,
)

# Text embedding


class TextEmbedding(nn.Module):
    def __init__(self, text_num_embeds, text_dim, conv_layers=0, conv_mult=2):
        super().__init__()
        self.text_embed = nn.Embedding(
            text_num_embeds + 1, text_dim
        )  # use 0 as filler token

        if conv_layers > 0:
            self.extra_modeling = True
            self.precompute_max_pos = 4096  # ~44s of 24khz audio
            self._freqs_cis = precompute_freqs_cis(text_dim, self.precompute_max_pos)
            self.text_blocks = nn.Sequential(
                *[
                    ConvNeXtV2Block(text_dim, text_dim * conv_mult)
                    for _ in range(conv_layers)
                ]
            )
        else:
            self.extra_modeling = False

    def __call__(self, text: int["b nt"], seq_len, drop_text=False):
        batch, text_len = text.shape[0], text.shape[1]
        text = (
            text + 1
        )  # use 0 as filler token. preprocess of batch pad -1, see list_str_to_idx()
        text = text[
            :, :seq_len
        ]  # curtail if character tokens are more than the mel spec tokens
        text = mx.pad(text, [(0, 0), (0, seq_len - text_len)], constant_values=0)

        if drop_text:  # cfg for text
            text = mx.zeros_like(text)

        text = self.text_embed(text)  # b n -> b n d

        # possible extra modeling
        if self.extra_modeling:
            # sinus pos emb
            batch_start = mx.zeros((batch,), dtype=mx.int32)
            pos_idx = get_pos_embed_indices(
                batch_start, seq_len, max_pos=self.precompute_max_pos
            )
            text_pos_embed = self._freqs_cis[pos_idx]
            text = text + text_pos_embed

            # convnextv2 blocks
            text = self.text_blocks(text)

        return text


# noised input audio and context mixing embedding


class InputEmbedding(nn.Module):
    def __init__(self, mel_dim, text_dim, out_dim):
        super().__init__()
        self.proj = nn.Linear(mel_dim * 2 + text_dim, out_dim)
        self.conv_pos_embed = ConvPositionEmbedding(dim=out_dim)

    def __call__(
        self,
        x: float["b n d"],
        cond: float["b n d"],
        text_embed: float["b n d"],
        drop_audio_cond=False,
    ):
        if drop_audio_cond:  # cfg for cond audio
            cond = mx.zeros_like(cond)

        x = self.proj(mx.concatenate((x, cond, text_embed), axis=-1))
        x = self.conv_pos_embed(x) + x
        return x


# AdaLayerNormZero
# return with modulated x for attn input, and params for later mlp modulation


class AdaLayerNormZero(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.silu = nn.SiLU()
        self.linear = nn.Linear(dim, dim * 6)
        self.norm = nn.LayerNorm(dim, affine=False, eps=1e-6)

    def __call__(self, x: mx.array, emb: mx.array | None = None) -> mx.array:
        emb = self.linear(self.silu(emb))
        shift_msa, scale_msa, gate_msa, shift_mlp, scale_mlp, gate_mlp = mx.split(
            emb, 6, axis=1
        )

        x = self.norm(x) * (1 + mx.expand_dims(scale_msa, axis=1)) + mx.expand_dims(
            shift_msa, axis=1
        )
        return x, gate_msa, shift_mlp, scale_mlp, gate_mlp


# AdaLayerNormZero for final layer
# return only with modulated x for attn input, cuz no more mlp modulation


class AdaLayerNormZero_Final(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.silu = nn.SiLU()
        self.linear = nn.Linear(dim, dim * 2)
        self.norm = nn.LayerNorm(dim, affine=False, eps=1e-6)

    def __call__(self, x: mx.array, emb: mx.array | None = None) -> mx.array:
        emb = self.linear(self.silu(emb))
        scale, shift = mx.split(emb, 2, axis=1)

        x = self.norm(x) * (1 + mx.expand_dims(scale, axis=1)) + mx.expand_dims(
            shift, axis=1
        )
        return x


# DiT block


class DiTBlock(nn.Module):
    def __init__(self, dim, heads, dim_head, ff_mult=4, dropout=0.1):
        super().__init__()

        self.attn_norm = AdaLayerNormZero(dim)
        self.attn = Attention(
            dim=dim,
            heads=heads,
            dim_head=dim_head,
            dropout=dropout,
        )

        self.ff_norm = nn.LayerNorm(dim, affine=False, eps=1e-6)
        self.ff = FeedForward(
            dim=dim, mult=ff_mult, dropout=dropout, approximate="tanh"
        )

    def __call__(
        self, x, t, mask=None, rope=None
    ):  # x: noised input, t: time embedding
        # pre-norm & modulation for attention input
        norm, gate_msa, shift_mlp, scale_mlp, gate_mlp = self.attn_norm(x, emb=t)

        # attention
        attn_output = self.attn(x=norm, mask=mask, rope=rope)

        # process attention output for input x
        x = x + mx.expand_dims(gate_msa, axis=1) * attn_output

        norm = self.ff_norm(x) * (
            1 + mx.expand_dims(scale_mlp, axis=1)
        ) + mx.expand_dims(shift_mlp, axis=1)
        ff_output = self.ff(norm)
        x = x + mx.expand_dims(gate_mlp, axis=1) * ff_output

        return x


# Transformer backbone using DiT blocks


class DiT(nn.Module):
    def __init__(
        self,
        *,
        dim,
        depth=8,
        heads=8,
        dim_head=64,
        dropout=0.0,
        ff_mult=4,
        mel_dim=100,
        text_num_embeds=256,
        text_dim=None,
        conv_layers=0,
        long_skip_connection=False,
    ):
        super().__init__()

        self.time_embed = TimestepEmbedding(dim)
        if text_dim is None:
            text_dim = mel_dim
        self.text_embed = TextEmbedding(
            text_num_embeds, text_dim, conv_layers=conv_layers
        )
        self.input_embed = InputEmbedding(mel_dim, text_dim, dim)

        self.rotary_embed = RotaryEmbedding(dim_head)

        self.dim = dim
        self.depth = depth

        self.transformer_blocks = [
            DiTBlock(
                dim=dim,
                heads=heads,
                dim_head=dim_head,
                ff_mult=ff_mult,
                dropout=dropout,
            )
            for _ in range(depth)
        ]
        self.long_skip_connection = (
            nn.Linear(dim * 2, dim, bias=False) if long_skip_connection else None
        )

        self.norm_out = AdaLayerNormZero_Final(dim)  # final modulation
        self.proj_out = nn.Linear(dim, mel_dim)

    def __call__(
        self,
        x: float["b n d"],  # nosied input audio
        cond: float["b n d"],  # masked cond audio
        text: int["b nt"],  # text
        time: float["b"] | float[""],  # time step
        drop_audio_cond,  # cfg for cond audio
        drop_text,  # cfg for text
        mask: bool["b n"] | None = None,
    ):
        batch, seq_len = x.shape[0], x.shape[1]
        if time.ndim == 0:
            time = repeat(time, " -> b", b=batch)

        # t: conditioning time, c: context (text + masked cond audio), x: noised input audio
        t = self.time_embed(time)
        text_embed = self.text_embed(text, seq_len, drop_text=drop_text)
        x = self.input_embed(x, cond, text_embed, drop_audio_cond=drop_audio_cond)

        rope = self.rotary_embed.forward_from_seq_len(seq_len)

        if self.long_skip_connection is not None:
            residual = x

        for block in self.transformer_blocks:
            x = block(x, t, mask=mask, rope=rope)

        if self.long_skip_connection is not None:
            x = self.long_skip_connection(mx.concatenate((x, residual), axis=-1))

        x = self.norm_out(x, t)
        output = self.proj_out(x)

        return output
