# Copyright (c) Malong LLC
# All rights reserved.
#
# Contact: github@malongtech.com
#
# This source code is licensed under the LICENSE file in the root directory of this source tree.

""" Architect controls architecture of cell by computing gradients of alpha """
import copy
import torch


class Architect():
    def __init__(self, net, teacher_net, w_momentum, w_weight_decay):
        self.net = net
        self.teacher_net = teacher_net
        self.v_net = copy.deepcopy(net)
        self.v_teacher_net = copy.deepcopy(teacher_net)
        self.w_momentum = w_momentum
        self.w_weight_decay = w_weight_decay
    
    def unrolled_backward(self, trn_X, trn_y, val_X, val_y, xi, w_optim):
        """ First Order!
            Compute unrolled loss and backward its gradients
        Args:
            xi: learning rate for virtual gradient step (same as net lr)
            w_optim: weights optimizer - for virtual step
        """
        logits_guide = self.teacher_net(val_X)
        logits = self.net(val_X)
        hard_loss, soft_loss, loss = self.net.criterion(logits, logits_guide, val_y, True)
        loss.backward()
        
        return hard_loss, soft_loss, loss

        logits_guide = self.v_teacher_net(val_X)
        logits = self.v_net(val_X)
        # hard_loss, soft_loss, loss = self.v_net.criterion(logits, logits_guide, val_y)
        loss = self.v_net.criterion.hard_criteria(logits, val_y)

        # hard_loss, soft_loss, loss = self.v_net.loss(val_X, logits_guide, val_y, return_detail=True)
        v_alphas = tuple(self.v_net.alphas())
        v_weights = tuple(self.v_net.weights())
        v_grads = torch.autograd.grad(loss, v_alphas + v_weights)
        dalpha = v_grads[:len(v_alphas)]

        with torch.no_grad():
            for alpha, da in zip(self.net.alphas(), dalpha):
                alpha.grad = da

        # return hard_loss, soft_loss, loss
        return loss
    
    def unrolled_backward_NONKD(self, trn_X, trn_y, val_X, val_y, xi, w_optim):
        """ First Order!
            Compute unrolled loss and backward its gradients
        Args:
            xi: learning rate for virtual gradient step (same as net lr)
            w_optim: weights optimizer - for virtual step
        """
        logits = self.net(val_X)
        loss = self.net.criterion.hard_criteria(logits, val_y)
        loss.backward()
        
        return loss

    def unrolled_backward_2nd(self, trn_X, trn_y, val_X, val_y, xi, w_optim):
        """ Second Order!
            Compute unrolled loss and backward its gradients
        Args:
            xi: learning rate for virtual gradient step (same as net lr)
            w_optim: weights optimizer - for virtual step
        """
        # do virtual step (calculate w`)
        self.virtual_step(trn_X, trn_y, xi, w_optim)

        # calculate unrolled loss
        logits_guide = self.v_teacher_net.forward(val_X)
        loss = self.v_net.loss(val_X, logits_guide, val_y)

        # compute gradient
        v_alphas = tuple(self.v_net.alphas())
        v_weights = tuple(self.v_net.weights())
        v_grads = torch.autograd.grad(loss, v_alphas + v_weights)
        dalpha = v_grads[:len(v_alphas)]
        dw = v_grads[len(v_alphas):]

        hessian = self.compute_hessian(dw, trn_X, trn_y)

        # update final gradient = dalpha - xi*hessian
        with torch.no_grad():
            for alpha, da, h in zip(self.net.alphas(), dalpha, hessian):
                alpha.grad = da - xi * h
    
    def virtual_step(self, trn_X, trn_y, xi, w_optim):
        """
        Compute unrolled weight w' (virtual step)

        Step process:
        1) forward
        2) calc loss
        3) compute gradient (by backprop)
        4) update gradient

        Args:
            xi: learning rate for virtual gradient step (same as weights lr)
            w_optim: weights optimizer
        """
        # forward & calc loss
        logits_guide = self.v_teacher_net.forward(trn_X)
        loss = self.net.loss(trn_X, logits_guide, trn_y)

        # compute gradient
        gradients = torch.autograd.grad(loss, self.net.weights())

        # do virtual step (update gradient)
        with torch.no_grad():
            for w, vw, g in zip(self.net.weights(), self.v_net.weights(), gradients):
                m = w_optim.state[w].get('momentum_buffer', 0.) * self.w_momentum
                vw.copy_(w - xi * (m + g + self.w_weight_decay * w))
            
            for a, va in zip(self.net.alphas(), self.v_net.alphas()):
                va.copy_(a)
        
    def compute_hessian(self, dw, trn_X, trn_y):
        """
        dw = dw` { L_val(w`, alpha) }
        w+ = w + eps * dw
        w- = w - eps * dw
        hessian = (dalpha { L_trn(w+, alpha) } - dalpha { L_trn(w-, alpha) }) / (2*eps)
        eps = 0.01 / ||dw||
        """
        norm = torch.cat([w.view(-1) for w in dw]).norm()
        eps = 0.01 / norm

        with torch.no_grad():
            for p, d in zip(self.net.weights(), dw):
                p += eps * d
        logits_guide = self.v_teacher_net.forward(trn_X)
        loss = self.net.loss(trn_X, logits_guide, trn_y)
        dalpha_pos = torch.autograd.grad(loss, self.net.alphas())

        with torch.no_grad():
            for p, d in zip(self.net.weights(), dw):
                p -= 2. * eps * d
        logits_guide = self.v_teacher_net.forward(trn_X)
        loss = self.net.loss(trn_X, logits_guide, trn_y)
        dalpha_neg = torch.autograd.grad(loss, self.net.alphas())

        with torch.no_grad():
            for p, d in zip(self.net.weights(), dw):
                p += eps * d
        
        hessian = [(p - n) / 2. * eps for p, n in zip(dalpha_pos, dalpha_neg)]
        return hessian

class Architect_Hint():
    def __init__(self, net, teacher_net, w_momentum, w_weight_decay, teacher_feature_extractor, Regressor):
        self.net = net
        self.teacher_net = teacher_net
        self.v_net = copy.deepcopy(net)
        self.v_teacher_net = copy.deepcopy(teacher_net)
        self.w_momentum = w_momentum
        self.w_weight_decay = w_weight_decay
        self.teacher_feature_extractor = teacher_feature_extractor
        self.Regressor = Regressor
        
    def unrolled_backward_hint(self, trn_X, trn_y, val_X, val_y, xi, w_optim, stage=1):
        """ First Order!
            Compute unrolled loss and backward its gradients
        Args:
            xi: learning rate for virtual gradient step (same as net lr)
            w_optim: weights optimizer - for virtual step
        """
        with torch.no_grad():
            teacher_hint_DICT = self.teacher_feature_extractor(val_X)
            
        student_features = self.net.extract_features(val_X, stage=stage)
        
        student_guided = self.Regressor(student_features["stage"+str(stage)], stage=stage)
        hint_loss = self.net.hint_criterion(student_guided, teacher_hint_DICT["stage"+str(stage)])
        hint_loss.backward()
        
        return hint_loss
    
    def unrolled_backward(self, trn_X, trn_y, val_X, val_y, xi, w_optim):
        """ First Order!
            Compute unrolled loss and backward its gradients
        Args:
            xi: learning rate for virtual gradient step (same as net lr)
            w_optim: weights optimizer - for virtual step
        """
        logits_guide = self.teacher_net(val_X)
        logits = self.net(val_X)
        hard_loss, soft_loss, loss = self.net.criterion(logits, logits_guide, val_y, True)
        loss.backward()
        
        return hard_loss, soft_loss, loss

        logits_guide = self.v_teacher_net(val_X)
        logits = self.v_net(val_X)
        # hard_loss, soft_loss, loss = self.v_net.criterion(logits, logits_guide, val_y)
        loss = self.v_net.criterion.hard_criteria(logits, val_y)

        # hard_loss, soft_loss, loss = self.v_net.loss(val_X, logits_guide, val_y, return_detail=True)
        v_alphas = tuple(self.v_net.alphas())
        v_weights = tuple(self.v_net.weights())
        v_grads = torch.autograd.grad(loss, v_alphas + v_weights)
        dalpha = v_grads[:len(v_alphas)]

        with torch.no_grad():
            for alpha, da in zip(self.net.alphas(), dalpha):
                alpha.grad = da

        # return hard_loss, soft_loss, loss
        return loss
    
    def unrolled_backward_NONKD(self, trn_X, trn_y, val_X, val_y, xi, w_optim):
        """ First Order!
            Compute unrolled loss and backward its gradients
        Args:
            xi: learning rate for virtual gradient step (same as net lr)
            w_optim: weights optimizer - for virtual step
        """
        logits = self.net(val_X)
        loss = self.net.criterion.hard_criteria(logits, val_y)
        loss.backward()
        
        return loss

    def unrolled_backward_2nd(self, trn_X, trn_y, val_X, val_y, xi, w_optim):
        """ Second Order!
            Compute unrolled loss and backward its gradients
        Args:
            xi: learning rate for virtual gradient step (same as net lr)
            w_optim: weights optimizer - for virtual step
        """
        # do virtual step (calculate w`)
        self.virtual_step(trn_X, trn_y, xi, w_optim)

        # calculate unrolled loss
        logits_guide = self.v_teacher_net.forward(val_X)
        loss = self.v_net.loss(val_X, logits_guide, val_y)

        # compute gradient
        v_alphas = tuple(self.v_net.alphas())
        v_weights = tuple(self.v_net.weights())
        v_grads = torch.autograd.grad(loss, v_alphas + v_weights)
        dalpha = v_grads[:len(v_alphas)]
        dw = v_grads[len(v_alphas):]

        hessian = self.compute_hessian(dw, trn_X, trn_y)

        # update final gradient = dalpha - xi*hessian
        with torch.no_grad():
            for alpha, da, h in zip(self.net.alphas(), dalpha, hessian):
                alpha.grad = da - xi * h
    
    def virtual_step(self, trn_X, trn_y, xi, w_optim):
        """
        Compute unrolled weight w' (virtual step)

        Step process:
        1) forward
        2) calc loss
        3) compute gradient (by backprop)
        4) update gradient

        Args:
            xi: learning rate for virtual gradient step (same as weights lr)
            w_optim: weights optimizer
        """
        # forward & calc loss
        logits_guide = self.v_teacher_net.forward(trn_X)
        loss = self.net.loss(trn_X, logits_guide, trn_y)

        # compute gradient
        gradients = torch.autograd.grad(loss, self.net.weights())

        # do virtual step (update gradient)
        with torch.no_grad():
            for w, vw, g in zip(self.net.weights(), self.v_net.weights(), gradients):
                m = w_optim.state[w].get('momentum_buffer', 0.) * self.w_momentum
                vw.copy_(w - xi * (m + g + self.w_weight_decay * w))
            
            for a, va in zip(self.net.alphas(), self.v_net.alphas()):
                va.copy_(a)
        
    def compute_hessian(self, dw, trn_X, trn_y):
        """
        dw = dw` { L_val(w`, alpha) }
        w+ = w + eps * dw
        w- = w - eps * dw
        hessian = (dalpha { L_trn(w+, alpha) } - dalpha { L_trn(w-, alpha) }) / (2*eps)
        eps = 0.01 / ||dw||
        """
        norm = torch.cat([w.view(-1) for w in dw]).norm()
        eps = 0.01 / norm

        with torch.no_grad():
            for p, d in zip(self.net.weights(), dw):
                p += eps * d
        logits_guide = self.v_teacher_net.forward(trn_X)
        loss = self.net.loss(trn_X, logits_guide, trn_y)
        dalpha_pos = torch.autograd.grad(loss, self.net.alphas())

        with torch.no_grad():
            for p, d in zip(self.net.weights(), dw):
                p -= 2. * eps * d
        logits_guide = self.v_teacher_net.forward(trn_X)
        loss = self.net.loss(trn_X, logits_guide, trn_y)
        dalpha_neg = torch.autograd.grad(loss, self.net.alphas())

        with torch.no_grad():
            for p, d in zip(self.net.weights(), dw):
                p += eps * d
        
        hessian = [(p - n) / 2. * eps for p, n in zip(dalpha_pos, dalpha_neg)]
        return hessian
