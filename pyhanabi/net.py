# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#
from collections import OrderedDict
import torch
import torch.nn as nn
from typing import Tuple, Dict, Optional
import common_utils
import math


@torch.jit.script
def duel(v: torch.Tensor, a: torch.Tensor, legal_move: torch.Tensor) -> torch.Tensor:
    assert a.size() == legal_move.size()
    # assert legal_move.dim() == 3  # seq, batch, dim
    legal_a = a * legal_move
    q = v + legal_a - legal_a.mean(-1, keepdim=True)
    return q


def cross_entropy(net, lstm_o, target_p, hand_slot_mask, seq_len):
    # target_p: [seq_len, batch, num_player, 5, 3]
    # hand_slot_mask: [seq_len, batch, num_player, 5]
    logit = net(lstm_o).view(target_p.size())
    q = nn.functional.softmax(logit, -1)
    logq = nn.functional.log_softmax(logit, -1)
    plogq = (target_p * logq).sum(-1)
    xent = -(plogq * hand_slot_mask).sum(-1) / hand_slot_mask.sum(-1).clamp(min=1e-6)

    if xent.dim() == 3:
        # [seq, batch, num_player]
        xent = xent.mean(2)

    # save before sum out
    seq_xent = xent
    xent = xent.sum(0)
    assert xent.size() == seq_len.size()
    avg_xent = (xent / seq_len).mean().item()
    return xent, avg_xent, q, seq_xent.detach()


class FFWDNet(torch.jit.ScriptModule):
    def __init__(self, in_dim, hid_dim, out_dim):
        super().__init__()
        # for backward compatibility
        if isinstance(in_dim, int):
            assert in_dim == 783
            self.in_dim = in_dim
            self.priv_in_dim = in_dim - 125
            self.publ_in_dim = in_dim - 2 * 125
        else:
            self.in_dim = in_dim
            self.priv_in_dim = in_dim[1]
            self.publ_in_dim = in_dim[2]

        self.hid_dim = hid_dim
        self.out_dim = out_dim

        self.net = nn.Sequential(
            nn.Linear(self.priv_in_dim, self.hid_dim),
            nn.ReLU(),
            nn.Linear(self.hid_dim, self.hid_dim),
            nn.ReLU(),
            nn.Linear(self.hid_dim, self.hid_dim),
            nn.ReLU(),
        )

        self.fc_v = nn.Linear(self.hid_dim, 1)
        self.fc_a = nn.Linear(self.hid_dim, self.out_dim)
        # for aux task
        self.pred_1st = nn.Linear(self.hid_dim, 5 * 3)

    @torch.jit.script_method
    def get_h0(self, batchsize: int) -> Dict[str, torch.Tensor]:
        """fake, only for compatibility"""
        shape = (1, batchsize, 1)
        hid = {"h0": torch.zeros(*shape), "c0": torch.zeros(*shape)}
        return hid

    @torch.jit.script_method
    def act(
        self, priv_s: torch.Tensor, publ_s: torch.Tensor, hid: Dict[str, torch.Tensor]
    ) -> Tuple[torch.Tensor, Dict[str, torch.Tensor]]:
        assert priv_s.dim() == 2, "dim should be 2, [batch, dim], get %d" % priv_s.dim()
        o = self.net(priv_s)
        a = self.fc_a(o)
        return a, hid

    @torch.jit.script_method
    def calculate_distribution(
        self,
        priv_s: torch.Tensor,
        legal_move: torch.Tensor,
    ) -> torch.Tensor:

        o = self.net(priv_s)
        a = self.fc_a(o)
        normed_a = a - a.mean(dim=-1,keepdim=True)
        q_prob = nn.functional.softmax(normed_a,dim=-1)
        # q: [(seq_len), batch, num_action]
        # action: [seq_len, batch]

        return q_prob

    @torch.jit.script_method
    def calculate_p(
        self,
        priv_s: torch.Tensor,
        legal_move: torch.Tensor,
        action: torch.Tensor,
    ) -> torch.Tensor:

        o = self.net(priv_s)
        a = self.fc_a(o)
        normed_a = a - a.mean(dim=-1,keepdim=True)
        q_prob = nn.functional.softmax(normed_a,dim=-1)
        # q: [(seq_len), batch, num_action]
        # action: [seq_len, batch]
        pa = q_prob.gather(-1, action.unsqueeze(-1)).squeeze(-1)

        return pa


    @torch.jit.script_method
    def forward(
        self,
        priv_s: torch.Tensor,
        publ_s: torch.Tensor,
        legal_move: torch.Tensor,
        action: torch.Tensor,
        hid: Dict[str, torch.Tensor],
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        assert (
            priv_s.dim() == 3 or priv_s.dim() == 2
        ), "dim = 3/2, [seq_len(optional), batch, dim]"

        o = self.net(priv_s)
        a = self.fc_a(o)
        v = self.fc_v(o)
        q = duel(v, a, legal_move)

        # q: [(seq_len), batch, num_action]
        # action: [seq_len, batch]
        qa = q.gather(-1, action.unsqueeze(-1)).squeeze(-1)

        assert q.size() == legal_move.size()
        legal_q = (1 + q - q.min()) * legal_move
        # greedy_action: [(seq_len), batch]
        greedy_action = legal_q.argmax(-1).detach()
        return qa, greedy_action, q, o

    def pred_loss_1st(self, o, target, hand_slot_mask, seq_len):
        return cross_entropy(self.pred_1st, o, target, hand_slot_mask, seq_len)

class LSTMNet(torch.jit.ScriptModule):
    __constants__ = ["hid_dim", "out_dim", "num_lstm_layer"]

    def __init__(self, device, in_dim, hid_dim, out_dim, num_lstm_layer, play_styles=0):
        super().__init__()
        # for backward compatibility
        if isinstance(in_dim, int):
            assert in_dim == 783
            self.in_dim = in_dim
            self.priv_in_dim = in_dim - 125
            self.publ_in_dim = in_dim - 2 * 125
        else:
            self.in_dim = in_dim
            self.priv_in_dim = in_dim[1]
            self.publ_in_dim = in_dim[2]
        self.play_styles = play_styles
        self.hid_dim = hid_dim
        self.out_dim = out_dim
        self.num_ff_layer = 1
        self.num_lstm_layer = num_lstm_layer
        ff_layers = [nn.Linear(self.priv_in_dim, self.hid_dim), nn.ReLU()]
        for i in range(1, self.num_ff_layer):
            ff_layers.append(nn.Linear(self.hid_dim, self.hid_dim))
            ff_layers.append(nn.ReLU())
        self.net = nn.Sequential(*ff_layers)

        self.lstm = nn.LSTM(
            self.hid_dim,
            self.hid_dim,
            num_layers=self.num_lstm_layer,
        ).to(device)
        self.lstm.flatten_parameters()
        self.fc_v = nn.Linear(self.hid_dim, 1)
        if self.play_styles == 0:
            self.fc_a = nn.Linear(self.hid_dim, self.out_dim)
            self.fc_as = nn.ModuleList([nn.Linear(2, 2)]) # to keep consistensy
        else:
            self.fc_a = nn.Linear(2, 2)
            self.fc_as = nn.ModuleList([nn.Linear(self.hid_dim, self.out_dim) for _ in range(self.play_styles)])

        # for aux task
        self.pred_1st = nn.Linear(self.hid_dim, 5 * 3)

    @torch.jit.script_method
    def get_h0(self, batchsize: int) -> Dict[str, torch.Tensor]:
        shape = (self.num_lstm_layer, batchsize, self.hid_dim)
        hid = {"h0": torch.zeros(*shape), "c0": torch.zeros(*shape)}
        return hid

    @torch.jit.script_method
    def act(
        self,
        priv_s: torch.Tensor,
        publ_s: torch.Tensor,
        playstyle_s: torch.Tensor,
        hid: Dict[str, torch.Tensor],
    ) -> Tuple[torch.Tensor, Dict[str, torch.Tensor]]:
        assert priv_s.dim() == 2 # batchsize, input_size

        bsize = hid["h0"].size(0)
        assert hid["h0"].dim() == 4
        # hid size: [batch, num_layer, num_player, dim]
        # -> [num_layer, batch x num_player, dim]
        hid = {
            "h0": hid["h0"].transpose(0, 1).flatten(1, 2).contiguous(),
            "c0": hid["c0"].transpose(0, 1).flatten(1, 2).contiguous(),
        }
        priv_s = priv_s.unsqueeze(0)
        x = self.net(priv_s)
        o, (h, c) = self.lstm(x, (hid["h0"], hid["c0"])) # o shape: [1, batch, dim]
        if self.play_styles == 0:
            a = self.fc_a(o)
            a = a.squeeze(0)
        else:
            o = o.squeeze(0)
            a_group = [module(o) for i, module in enumerate(self.fc_as)]
            a_stack = torch.stack(a_group, dim=-1) # shape: [batch, num_action, num_playstyle]
            assert playstyle_s.dim() == 1 # [batch,1]
            onehot_playstyle = nn.functional.one_hot(playstyle_s,num_classes=self.play_styles).float().unsqueeze(-2) # [batch, 1, num_playstyle]
            a = (a_stack * onehot_playstyle).sum(dim=-1) # shape: [batch, num_action]
        # hid size: [num_layer, batch x num_player, dim]
        # -> [batch, num_layer, num_player, dim]
        interim_hid_shape = (
            self.num_lstm_layer,
            bsize,
            -1,
            self.hid_dim,
        )
        h = h.view(*interim_hid_shape).transpose(0, 1)
        c = c.view(*interim_hid_shape).transpose(0, 1)

        return a, {"h0": h, "c0": c}

    @torch.jit.script_method
    def calculate_distribution(
        self,
        priv_s: torch.Tensor,
        legal_move: torch.Tensor,
        hid: Dict[str, torch.Tensor],
    ) -> torch.Tensor:

        x = self.net(priv_s)
        o, _ = self.lstm(x, (hid["h0"], hid["c0"]))
        a = self.fc_a(o)
        legal_a = a*legal_move
        normed_a = legal_a - legal_a.mean(dim=-1,keepdim=True)
        q_prob = nn.functional.softmax(normed_a,dim=-1)

        return q_prob

    @torch.jit.script_method
    def calculate_p(
        self,
        priv_s: torch.Tensor,
        legal_move: torch.Tensor,
        action: torch.Tensor,
        hid: Dict[str, torch.Tensor],
    ) -> torch.Tensor:

        x = self.net(priv_s)
        o, _ = self.lstm(x, (hid["h0"], hid["c0"]))
        a = self.fc_a(o)
        legal_a = (0.1+a-a.min())*legal_move
        normed_a = legal_a - legal_a.mean(dim=-1,keepdim=True)
        q_prob = nn.functional.softmax(normed_a,dim=-1)
        # q: [(seq_len), batch, num_action]
        # action: [seq_len, batch]
        pa = q_prob.gather(-1, action.unsqueeze(-1)).squeeze(-1)

        return pa

    @torch.jit.script_method
    def forward(
        self,
        priv_s: torch.Tensor,
        publ_s: torch.Tensor,
        playstyle_s: torch.Tensor,
        legal_move: torch.Tensor,
        action: torch.Tensor,
        hid: Dict[str, torch.Tensor],
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        assert (
            priv_s.dim() == 3 or priv_s.dim() == 2
        ), "dim = 3/2, [seq_len(optional), batch, dim]"

        one_step = False
        if priv_s.dim() == 2:
            priv_s = priv_s.unsqueeze(0)
            publ_s = publ_s.unsqueeze(0)
            legal_move = legal_move.unsqueeze(0)
            action = action.unsqueeze(0)
            one_step = True

        x = self.net(priv_s)
        if len(hid) == 0:
            o, _ = self.lstm(x)
        else:
            o, _ = self.lstm(x, (hid["h0"], hid["c0"]))
        if self.play_styles == 0:
            a = self.fc_a(o) # shape: [seq_len, batch, num_action]
            v = self.fc_v(o)
            a_stack_ret = a # to keep return value consistent with multi-playstyle case 
        else:
            a_group = [module(o) for i, module in enumerate(self.fc_as)]
            a_stack = torch.stack(a_group, dim=-1) # shape: [seq_len, batch, num_action, num_playstyle]
            assert a_stack.dim() == 4
            onehot_playstyle = nn.functional.one_hot(playstyle_s,num_classes=self.play_styles).float().unsqueeze(-2) # [seq_len, batch,1, num_playstyle]
            a = (a_stack * onehot_playstyle).sum(dim=-1) # shape: [seq_len, batch, num_action]
            v = self.fc_v(o)
            a_stack_ret = (a_stack + 1 - a_stack.min()) * legal_move.unsqueeze(-1)
        q = duel(v, a, legal_move)

        # q: [seq_len, batch, num_action]
        # action: [seq_len, batch]
        qa = q.gather(2, action.unsqueeze(2)).squeeze(2)

        assert q.size() == legal_move.size()
        legal_q = (1 + q - q.min()) * legal_move
        # greedy_action: [seq_len, batch]
        greedy_action = legal_q.argmax(2).detach()
        if one_step:
            qa = qa.squeeze(0)
            greedy_action = greedy_action.squeeze(0)
            o = o.squeeze(0)
            q = q.squeeze(0)
        return qa, greedy_action, q, o, a_stack_ret

    def pred_loss_1st(self, lstm_o, target, hand_slot_mask, seq_len):
        return cross_entropy(self.pred_1st, lstm_o, target, hand_slot_mask, seq_len)


class PublicLSTMNet(torch.jit.ScriptModule):
    __constants__ = ["hid_dim", "out_dim", "num_lstm_layer"]

    def __init__(self, device, in_dim, hid_dim, out_dim, num_lstm_layer):
        super().__init__()
        # for backward compatibility
        if isinstance(in_dim, int):
            assert in_dim == 783
            self.in_dim = in_dim
            self.priv_in_dim = in_dim - 125
            self.publ_in_dim = in_dim - 2 * 125
        else:
            self.in_dim = in_dim
            self.priv_in_dim = in_dim[1]
            self.publ_in_dim = in_dim[2]

        self.hid_dim = hid_dim
        self.out_dim = out_dim
        self.num_ff_layer = 1
        self.num_lstm_layer = num_lstm_layer

        self.priv_net = nn.Sequential(
            nn.Linear(self.priv_in_dim, self.hid_dim),
            nn.ReLU(),
            nn.Linear(self.hid_dim, self.hid_dim),
            nn.ReLU(),
            nn.Linear(self.hid_dim, self.hid_dim),
            nn.ReLU(),
        )

        ff_layers = [nn.Linear(self.publ_in_dim, self.hid_dim), nn.ReLU()]
        for i in range(1, self.num_ff_layer):
            ff_layers.append(nn.Linear(self.hid_dim, self.hid_dim))
            ff_layers.append(nn.ReLU())
        self.publ_net = nn.Sequential(*ff_layers)

        self.lstm = nn.LSTM(
            self.hid_dim,
            self.hid_dim,
            num_layers=self.num_lstm_layer,
        ).to(device)
        self.lstm.flatten_parameters()

        self.fc_v = nn.Linear(self.hid_dim, 1)
        self.fc_a = nn.Linear(self.hid_dim, self.out_dim)

        # for aux task
        self.pred_1st = nn.Linear(self.hid_dim, 5 * 3)

    @torch.jit.script_method
    def get_h0(self, batchsize: int) -> Dict[str, torch.Tensor]:
        shape = (self.num_lstm_layer, batchsize, self.hid_dim)
        hid = {"h0": torch.zeros(*shape), "c0": torch.zeros(*shape)}
        return hid

    @torch.jit.script_method
    def calculate_distribution(
        self,
        priv_s: torch.Tensor,
        legal_move: torch.Tensor,
    ) -> torch.Tensor:

        o = self.net(priv_s)
        a = self.fc_a(o)
        normed_a = a - a.mean(dim=-1,keepdim=True)
        q_prob = nn.functional.softmax(normed_a,dim=-1)
        # q: [(seq_len), batch, num_action]
        # action: [seq_len, batch]

        return q_prob

    @torch.jit.script_method
    def calculate_p(
        self,
        priv_s: torch.Tensor,
        legal_move: torch.Tensor,
        action: torch.Tensor,
    ) -> torch.Tensor:

        o = self.net(priv_s)
        a = self.fc_a(o)
        normed_a = a - a.mean(dim=-1,keepdim=True)
        q_prob = nn.functional.softmax(normed_a,dim=-1)
        # q: [(seq_len), batch, num_action]
        # action: [seq_len, batch]
        pa = q_prob.gather(-1, action.unsqueeze(-1)).squeeze(-1)

        return pa

    @torch.jit.script_method
    def act(
        self,
        priv_s: torch.Tensor,
        publ_s: torch.Tensor,
        hid: Dict[str, torch.Tensor],
    ) -> Tuple[torch.Tensor, Dict[str, torch.Tensor]]:
        assert priv_s.dim() == 2

        bsize = hid["h0"].size(0)
        assert hid["h0"].dim() == 4
        # hid size: [batch, num_layer, num_player, dim]
        # -> [num_layer, batch x num_player, dim]
        hid = {
            "h0": hid["h0"].transpose(0, 1).flatten(1, 2).contiguous(),
            "c0": hid["c0"].transpose(0, 1).flatten(1, 2).contiguous(),
        }

        priv_s = priv_s.unsqueeze(0)
        publ_s = publ_s.unsqueeze(0)

        x = self.publ_net(publ_s)
        publ_o, (h, c) = self.lstm(x, (hid["h0"], hid["c0"]))

        priv_o = self.priv_net(priv_s)
        o = priv_o * publ_o
        a = self.fc_a(o)
        a = a.squeeze(0)

        # hid size: [num_layer, batch x num_player, dim]
        # -> [batch, num_layer, num_player, dim]
        interim_hid_shape = (
            self.num_lstm_layer,
            bsize,
            -1,
            self.hid_dim,
        )
        h = h.view(*interim_hid_shape).transpose(0, 1)
        c = c.view(*interim_hid_shape).transpose(0, 1)

        return a, {"h0": h, "c0": c}

    @torch.jit.script_method
    def forward(
        self,
        priv_s: torch.Tensor,
        publ_s: torch.Tensor,
        legal_move: torch.Tensor,
        action: torch.Tensor,
        hid: Dict[str, torch.Tensor],
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        assert (
            priv_s.dim() == 3 or priv_s.dim() == 2
        ), "dim = 3/2, [seq_len(optional), batch, dim]"

        one_step = False
        if priv_s.dim() == 2:
            priv_s = priv_s.unsqueeze(0)
            publ_s = publ_s.unsqueeze(0)
            legal_move = legal_move.unsqueeze(0)
            action = action.unsqueeze(0)
            one_step = True

        x = self.publ_net(publ_s)
        if len(hid) == 0:
            publ_o, _ = self.lstm(x)
        else:
            publ_o, _ = self.lstm(x, (hid["h0"], hid["c0"]))
        priv_o = self.priv_net(priv_s)
        o = priv_o * publ_o
        a = self.fc_a(o)
        v = self.fc_v(o)
        q = duel(v, a, legal_move)

        # q: [seq_len, batch, num_action]
        # action: [seq_len, batch]
        qa = q.gather(2, action.unsqueeze(2)).squeeze(2)

        assert q.size() == legal_move.size()
        legal_q = (1 + q - q.min()) * legal_move
        # greedy_action: [seq_len, batch]
        greedy_action = legal_q.argmax(2).detach()

        if one_step:
            qa = qa.squeeze(0)
            greedy_action = greedy_action.squeeze(0)
            o = o.squeeze(0)
            q = q.squeeze(0)
        return qa, greedy_action, q, o

    def pred_loss_1st(self, lstm_o, target, hand_slot_mask, seq_len):
        return cross_entropy(self.pred_1st, lstm_o, target, hand_slot_mask, seq_len)
