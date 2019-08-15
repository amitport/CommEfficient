import ray
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import numpy as np

from minimal import CSVec

from sketched_classes import SketchedLossResult, SketchedParamGroup

@ray.remote(num_gpus=1)
class FedParamServer:
    def __init__(self, args,
    	sketch_params_larger_than=0, sketch_biases=False):
        self.k = args['k']
        self.p2 = args['p2']
        self.num_cols = args['num_cols']
        self.num_rows = args['num_rows']
        self.num_blocks = args['num_blocks']
        self.rounds = []
        self.sketch_params_larger_than = sketch_params_larger_than
        self.sketch_biases = sketch_biases
        self.device = torch.device("cuda" if 
            torch.cuda.is_available() else "cpu")

    def sync(self, round_id):
        w_stale = self.rounds[round_id]
        w_new = self.rounds[-1]
        desired_diff = w_new - w_stale
        # find a datum such that the gradient of the model evaluated
        # on that datum using w_stale equals desired_diff
        #raise RuntimeWarning("overriding your model's requires_grad " + \
        #                     "due to extreme laziness")
        def get_param_vec(model):
            param_vec = []
            start = 0
            for p in model.parameters():
                end = start + p.numel()
                param_vec.append(p.data.view(-1))
                start = end
            return torch.cat(param_vec).to(self.device)

        def set_param_vec(model, param_vec):
            start = 0
            for p in model.parameters():
                end = start + p.numel()
                p.data.zero_()
                p.data.add_(param_vec[start:end].view(p.size()))
                start = end

        def get_grad_vec(model):
            start = 0
            grad_vec = []
            for p in model.parameters():
                end = start + p.numel()
                if p.grad is None:
                    grad_vec.append(torch.zeros(p.numel()))
                else:
                    grad_vec.append(p.grad.view(-1))
                start = end
            return torch.cat(grad_vec).to(self.device)

        def set_grad_vec(model, grad_vec):
            start = 0
            for p in model.parameters():
                end = start + p.numel()
                p.grad.data.zero_()
                p.data.add_(grad_vec[start:end].view(p.grad.size()))
                start = end

        orig_param_vec = get_param_vec(self.model)
        set_param_vec(self.model, w_stale)

        d = torch.randn(1, 3, 32, 32).to(self.device)
        d.requires_grad = True

        # all workers know to use class 0 as the target
        fake_target = torch.tensor([0]).long().to(self.device)

        # save the original gradient
        orig_grad = get_grad_vec(self.model)

        # minimize ||model_grad - desired_diff||_2^2 over d
        opt = torch.optim.SGD((d,), lr=0.01, momentum=0.9)
        for i in range(10):
            opt.zero_grad()
            self.model.zero_grad()
            fake_loss = self.criterion(self.model(d), fake_target)
            fake_loss.backward(create_graph=True)
            model_grad = get_grad_vec(self.model)

            loss = torch.sum((model_grad - desired_diff)**2)
            data_grad = torch.autograd.grad(loss, d, only_inputs=True)[0]

            opt.param_groups[0]["params"][0].grad = data_grad
            opt.step()

        # reset to original state
        set_grad_vec(self.model, orig_grad)
        set_param_vec(self.model, orig_param_vec)

        return w_stale

    def all_reduce_sketched(self, *grads):
        # compute update
        """
        grads = [grad.to(self.device) for grad in grads]
        self._apply_update(torch.mean(torch.stack(grads), dim=0))
        return
        """
        #self.cuda()
        self.sketch.zero()
        for grad in grads:
            self.sketch += grad[self.sketch_mask]
        candidate_top_k = self.sketch.unSketch(k=self.p2*self.k)
        candidate_hh_coords = candidate_top_k.nonzero()
        hhs = [grad[candidate_hh_coords] for grad in grads]
        candidate_top_k[candidate_hh_coords] = torch.sum(
            torch.stack(hhs),dim=0)
        weights = self._topk(candidate_top_k, k=self.k)
        weight_update = torch.zeros(self.grad_size, device=self.device)
        weight_update[self.sketch_mask] = weights
        weight_update[~self.sketch_mask] = torch.sum(
            torch.stack(
                [grad[~self.sketch_mask] for grad in grads]), dim=0)
        self._apply_update(weight_update)
        #self.cpu()
        #"""

    def _apply_update(self, update):
    	curr_weights = self.rounds[-1]
    	weight_update = curr_weights - update
    	self.rounds.append(weight_update)
        #self.sync(weightUpdate * self._getLRVec())
        # weight_update = update * self._getLRVec()
        # #import pdb; pdb.set_trace()
        # weight_update = weight_update.to(self.device)
        # start = 0
        # for param_group in self.param_groups:
        #     for p in param_group['params']:
        #         end = start + torch.numel(p)
        #         p.data.add_(-weight_update[start:end].reshape(p.data.shape))
        #         start = end
        #import pdb; pdb.set_trace()
        # self._setGradVec(weight_update)
        # self._updateParamsWithGradVec()

    def _topk(self, vec, k):
        """ Return the largest k elements (by magnitude) of vec"""
        ret = torch.zeros_like(vec)
        # on a gpu, sorting is faster than pytorch's topk method
        topkIndices = torch.sort(vec**2)[1][-k:]
        ret[topkIndices] = vec[topkIndices]
        return ret

    def set_optimizer(self, opt):
        assert self.model is not None, \
        "model must be already initialized"
        p = opt.param_groups[0]
        lr = p['lr']
        dampening = p['dampening']
        nesterov = p['nesterov']
        weight_decay = p['weight_decay']
        momentum = p['momentum']
        opt = optim.SGD(self.model.parameters(), 
            lr=lr, 
            dampening=dampening, 
            nesterov=nesterov, 
            weight_decay=weight_decay, 
            momentum=momentum)
        # del self.model
        self.param_groups = opt.param_groups
        self.grad_size = 0
        sketch_mask = []
        weight_vec = torch.tensor([]).to(self.device)
        for group in self.param_groups:
            for p in group["params"]:
                if p.requires_grad:
                    size = torch.numel(p)
                    if p.do_sketching:
                        sketch_mask.append(torch.ones(size))
                    else:
                        sketch_mask.append(torch.zeros(size))
                    #weight_vec.append(p.data.view(-1).float())
                    d = p.data.view(-1).float()
                    weight_vec = torch.cat((weight_vec, d), dim=0) 
                    self.grad_size += size
        # del self.param_groups
        self.rounds.append(weight_vec)
        self.sketch_mask = torch.cat(sketch_mask).byte().to(self.device)
        self.sketch = CSVec(d=self.sketch_mask.sum().item(), 
            c=self.num_cols,
            r=self.num_rows,
            device=self.device,
            nChunks=1,
            numBlocks=self.num_blocks)
        print(f"Total dimension is {self.grad_size} using k {self.k} and p2 {self.p2} with sketch_mask.sum(): {self.sketch_mask.sum()}")

    def set_model(self, model_cls, model_config, 
            sketch_biases, sketch_params_larger_than):
        rand_state = torch.random.get_rng_state()
        torch.random.manual_seed(42)
        model = model_cls(**model_config).to(self.device)
        torch.random.set_rng_state(rand_state)
        for p in model.parameters():
            p.do_sketching = p.numel() >= sketch_params_larger_than
        # override bias terms with whatever sketchBiases is
        for m in model.modules():
            if isinstance(m, torch.nn.Linear):
                if m.bias is not None:
                    m.bias.do_sketching = sketch_biases
        self.model = model.to(self.device)

    def model_call(self, *args):
        #self.cuda()
        outs = self.model(args[0].to(self.device))
        #print(f"Length of self.outs is {len(self.outs)}")
        return outs

    def set_loss(self, criterion):
        self.criterion = criterion.to(self.device)

    def loss_call(self, *args):
        loss = self.criterion(
            args[0].to(self.device), 
            args[1].to(self.device))
        #self.cpu()
        return loss

    def get_param_groups(self):
        try:
            return [{'initial_lr': group['initial_lr'],
             'lr': group['lr']} for group in self.param_groups]
        except Exception as e:
            #print(f"Exception is {e}")
            return [{'lr': group['lr']} for group in self.param_groups]

