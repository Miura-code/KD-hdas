# Copyright (c) Malong LLC
# All rights reserved.
#
# Contact: github@malongtech.com
#
# This source code is licensed under the LICENSE file in the root directory of this source tree.

""" CNN DAG for architecture search """
from collections import OrderedDict
from models.get_cell import GetCell, Get_StageSpecified_Cell
import os
import torch
import logging
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.parallel._functions import Broadcast

import genotypes.genotypes as gt
from models.search_bigDAG import SearchBigDAG, SearchBigDAG_CS, SearchBigDAG_FullCascade, SearchBigDAGPartiallyConnection
from utils import setting


def broadcast_list(l, device_ids):
    """ Broadcasting list """
    l_copies = Broadcast.apply(device_ids, *l)
    l_copies = [l_copies[i: i + len(l)] for i in range(0, len(l_copies), len(l))]
    return l_copies


class SearchStage(nn.Module):
    """
    DAG for search
    Each edge is mixed and continuous relaxed
    """
    def __init__(self, input_size, C_in, C, n_classes, n_layers, genotype, n_big_nodes, stem_multiplier=4, cell_multiplier=4, spec_cell=False, slide_window=3):
        """
        C_in: # of input channels
        C: # of starting model channels
        n_classes: # of classes
        n_layers: # of layers
        n_big_nodes: # of intermediate n_cells  # 6
        genotype: the shape of normal cell and reduce cell
        """
        super().__init__()
        self.C_in = C_in
        self.C = C
        self.n_classes = n_classes
        self.n_layers = n_layers
        self.genotype = genotype
        self.n_big_nodes = n_big_nodes
        self.window = slide_window

        C_cur = stem_multiplier * C  # 4 * 16 = 64
        if input_size == setting.IMAGENET_SIZE:
            self.stem0 = nn.Sequential(
                nn.Conv2d(3, C // 2, kernel_size=3, stride=2, padding=1, bias=False),
                nn.BatchNorm2d(C // 2),
                nn.ReLU(inplace=True),
                nn.Conv2d(C // 2, C, 3, stride=2, padding=1, bias=False),
                nn.BatchNorm2d(C),
            )
            self.stem1 = nn.Sequential(
                nn.ReLU(inplace=True),
                nn.Conv2d(C, C_cur, 3, stride=2, padding=1, bias=False),
                nn.BatchNorm2d(C_cur),
            )
            self.stem = nn.Sequential(
                OrderedDict(
                    [
                        ("stem0", self.stem0),
                        ("stem1", self.stem1)
                    ]
                )
            )
        else:
            self.stem0 = nn.Sequential(
                nn.Conv2d(C_in, C_cur, 3, 1, 1, bias=False),
                nn.BatchNorm2d(C_cur)
            )
            self.stem = nn.Sequential(
                OrderedDict(
                    [
                        ("stem0", self.stem0),
                    ]
                )
            )
        C_pp, C_p, C_cur = cell_multiplier * C, cell_multiplier * C, C

        self.cells = nn.ModuleList()

        for i in range(n_layers):
            if i in range(n_layers // 3):
                reduction = False
                cell = GetCell(genotype, C_pp, C_p, C_cur, reduction) if not spec_cell else Get_StageSpecified_Cell(genotype, C_pp, C_p, C_cur, False, reduction, i, n_layers)
                self.cells.append(cell)
            if i in [n_layers // 3]:
                self.bigDAG1 = SearchBigDAG(n_big_nodes, self.cells, 0, n_layers // 3, stem_multiplier*C_cur, stem_multiplier*C_cur, cell_multiplier * C_cur, window=self.window)

                reduction = True
                C_pp = C_p = 2*cell.multiplier*C_cur
                C_cur *= 2
                cell = GetCell(genotype, C_pp, C_p, C_cur, reduction) if not spec_cell else Get_StageSpecified_Cell(genotype, C_pp, C_p, C_cur, False, reduction, i, n_layers)
                self.cells.append(cell)
            if i in range(n_layers // 3 + 1, 2 * n_layers // 3):
                reduction = False
                cell = GetCell(genotype, C_pp, C_p, C_cur, reduction) if not spec_cell else Get_StageSpecified_Cell(genotype, C_pp, C_p, C_cur, False, reduction, i, n_layers)
                self.cells.append(cell)
            if i in [2 * n_layers // 3]:
                self.bigDAG2 = SearchBigDAG(n_big_nodes, self.cells, n_layers // 3 + 1, 2 * n_layers // 3, C_pp, C_p, cell_multiplier * C_cur, window=self.window)

                reduction = True
                C_pp = C_p = 2*cell.multiplier*C_cur
                C_cur *= 2
                cell = GetCell(genotype, C_pp, C_p, C_cur, reduction) if not spec_cell else Get_StageSpecified_Cell(genotype, C_pp, C_p, C_cur, False, reduction, i, n_layers)
                self.cells.append(cell)
            if i in range(2 * n_layers // 3 + 1, n_layers):
                reduction = False
                cell = GetCell(genotype, C_pp, C_p, C_cur, reduction) if not spec_cell else Get_StageSpecified_Cell(genotype, C_pp, C_p, C_cur, False, reduction, i, n_layers)
                self.cells.append(cell)

            C_pp, C_p = cell_multiplier*C_cur, cell_multiplier*C_cur


        # self.bigDAG1 = SearchBigDAG(n_big_nodes, self.cells, 0, n_layers // 3, 4 * C)
        # self.bigDAG2 = SearchBigDAG(n_big_nodes, self.cells, n_layers // 3 + 1, 2 * n_layers // 3, 8 * C)
        self.bigDAG3 = SearchBigDAG(n_big_nodes, self.cells, 2 * n_layers // 3 + 1, n_layers, C_pp, C_p, cell_multiplier * C_cur, window=self.window)

        self.gap = nn.AdaptiveAvgPool2d(1)
        self.linear = nn.Linear(32 * C, n_classes)
    
    def forward(self, x, weights_DAG):
        s0 = s1 = self.stem(x)
        s0 = s1 = self.bigDAG1(s0, s1, weights_DAG[0 * self.n_big_nodes: 1 * self.n_big_nodes])
        s0 = s1 = self.cells[1 * self.n_layers // 3](s0, s1)
        s0 = s1 = self.bigDAG2(s0, s1, weights_DAG[1 * self.n_big_nodes: 2 * self.n_big_nodes])
        s0 = s1 = self.cells[2 * self.n_layers // 3](s0, s1)
        s0 = s1 = self.bigDAG3(s0, s1, weights_DAG[2 * self.n_big_nodes: 3 * self.n_big_nodes])

        out = self.gap(s1)
        out = out.view(out.size(0), -1)
        logits = self.linear(out)
        return logits


class SearchStageController(nn.Module):
    """ SearchDAG controller supporting multi-gpu """
    def __init__(self, input_size, C_in, C, n_classes, n_layers, criterion, genotype, stem_multiplier=4, device_ids=None, spec_cell=False, slide_window=3):
        super().__init__()
        self.n_big_nodes = n_layers // 3
        self.criterion = criterion
        self.genotype = genotype
        self.n_classes = n_classes
        self.n_layers = n_layers
        self.window = slide_window
        if device_ids is None:
            device_ids = list(range(torch.cuda.device_count()))
        self.device_ids = device_ids

        n_ops = len(gt.PRIMITIVES2)

        self.alpha_DAG = nn.ParameterList()

        # 3 stages
        # initialize architecture parameter(alpha)
        for _ in range(3):
            for i in range(self.n_big_nodes):
                # sliding window
                if i + 2 < self.window:
                    self.alpha_DAG.append(nn.Parameter(1e-3 * torch.randn(i + 2, n_ops)))
                else:
                    self.alpha_DAG.append(nn.Parameter(1e-3 * torch.randn(self.window, n_ops)))
        
        self._alphas = []
        for n, p in self.named_parameters():
            if 'alpha' in n:
                self._alphas.append((n, p))
        self.net = SearchStage(input_size, C_in, C, n_classes, n_layers, genotype, self.n_big_nodes, stem_multiplier=stem_multiplier, spec_cell=spec_cell, slide_window=self.window)
          
    def forward(self, x):
        weights_DAG = [F.softmax(alpha, dim=-1) for alpha in self.alpha_DAG]

        if len(self.device_ids) == 1:
            return self.net(x, weights_DAG)
        
        xs = nn.parallel.scatter(x, self.device_ids)
        wDAG_copies = broadcast_list(weights_DAG, self.device_ids)
        replicas = nn.parallel.replicate(self.net, self.device_ids)
        outputs = nn.parallel.parallel_apply(replicas,
                                             list(zip(xs, wDAG_copies)),
                                             devices=self.device_ids)
        return nn.parallel.gather(outputs, self.device_ids[0])
    
    def loss(self, X, y):
        logits = self.forward(X)
        return self.criterion(logits, y)
    
    def print_alphas(self, logger):
        org_formatters = []
        for handler in logger.handlers:
            org_formatters.append(handler.formatter)
            handler.setFormatter(logging.Formatter("%(message)s"))
        
        logger.info("####### ALPHA #######")
        logger.info("# Alpha - DAG")
        for alpha in self.alpha_DAG:
            logger.info(F.softmax(alpha, dim=-1))
        logger.info("#####################")

        for handler, formatter in zip(logger.handlers, org_formatters):
            handler.setFormatter(formatter)
    
    def DAG(self):
        gene_DAG1 = gt.parse(self.alpha_DAG[0 * self.n_big_nodes: 1 * self.n_big_nodes], k=2, window=self.window)
        gene_DAG2 = gt.parse(self.alpha_DAG[1 * self.n_big_nodes: 2 * self.n_big_nodes], k=2, window=self.window)
        gene_DAG3 = gt.parse(self.alpha_DAG[2 * self.n_big_nodes: 3 * self.n_big_nodes], k=2, window=self.window)

        concat = range(self.n_big_nodes, self.n_big_nodes + 2)

        return gt.Genotype2(DAG1=gene_DAG1, DAG1_concat=concat,
                            DAG2=gene_DAG2, DAG2_concat=concat,
                            DAG3=gene_DAG3, DAG3_concat=concat)
    
    def weights(self):
        return self.net.parameters()
    
    def named_weights(self):
        return self.net.named_parameters()
    
    def alphas(self):
        for n, p in self._alphas:
            yield p
    
    def named_alphas(self):
        for n, p in self._alphas:
            yield n, p
      
class SearchShareStage(SearchStage):
    """
    DAG for search
    Each edge is mixed and continuous relaxed
    """
    def __init__(self, C_in, C, n_classes, n_layers, genotype, n_big_nodes, stem_multiplier, spec_cell, slide_window=3):
        """
        C_in: # of input channels
        C: # of starting model channels
        n_classes: # of classes
        n_layers: # of layers
        n_big_nodes: # of intermediate n_cells  # 6
        genotype: the shape of normal cell and reduce cell
        """
        super().__init__(C_in, C, n_classes, n_layers, genotype, n_big_nodes, stem_multiplier, spec_cell, slide_window=slide_window)
    
    def forward(self, x, weights_DAG):
        s0 = s1 = self.stem(x)
        s0 = s1 = self.bigDAG1(s0, s1, weights_DAG)
        s0 = s1 = self.cells[1 * self.n_layers // 3](s0, s1)
        s0 = s1 = self.bigDAG2(s0, s1, weights_DAG)
        s0 = s1 = self.cells[2 * self.n_layers // 3](s0, s1)
        s0 = s1 = self.bigDAG3(s0, s1, weights_DAG)

        out = self.gap(s1)
        out = out.view(out.size(0), -1)
        logits = self.linear(out)
        return logits
            
class SearchShareStageController(SearchStageController):
    """ SearchDAG controller supporting multi-gpu """
    def __init__(self, C_in, C, n_classes, n_layers, criterion, genotype, stem_multiplier=4, device_ids=None, spec_cell=False, slide_window=3):
        super().__init__(C_in, C, n_classes, n_layers, criterion, genotype, stem_multiplier, device_ids=device_ids, spec_cell=spec_cell, slide_window=slide_window)
        
        n_ops = len(gt.PRIMITIVES2)

        self.alpha_DAG = nn.ParameterList()
        # There is one DAG shared for each stage
        # initialize architecture parameter(alpha)
        for i in range(self.n_big_nodes):
            # sliding window
            if i + 2 < self.window:
                self.alpha_DAG.append(nn.Parameter(1e-3 * torch.randn(i + 2, n_ops)))
            else:
                self.alpha_DAG.append(nn.Parameter(1e-3 * torch.randn(self.window, n_ops)))
        
        self._alphas = []
        for n, p in self.named_parameters():
            if 'alpha' in n:
                self._alphas.append((n, p))
                        
        self.net = SearchShareStage(C_in, C, n_classes, n_layers, genotype, self.n_big_nodes, stem_multiplier, spec_cell, self.window)
    
    def DAG(self):
        gene_DAG = gt.parse(self.alpha_DAG, k=2, window=self.window)
        # gene_DAG2 = gt.parse(self.alpha_DAG[1 * self.n_big_nodes: 2 * self.n_big_nodes], k=2)
        # gene_DAG3 = gt.parse(self.alpha_DAG[2 * self.n_big_nodes: 3 * self.n_big_nodes], k=2)

        concat = range(self.n_big_nodes, self.n_big_nodes + 2)

        return gt.Genotype2(DAG1=gene_DAG, DAG1_concat=concat,
                            DAG2=gene_DAG, DAG2_concat=concat,
                            DAG3=gene_DAG, DAG3_concat=concat)

class SearchStage_PartiallyConnected(SearchStage):
    def __init__(self, input_size, C_in, C, n_classes, n_layers, genotype, n_big_nodes, stem_multiplier=4, cell_multiplier=4, spec_cell=False, slide_window=3):
        super().__init__(input_size, C_in, C, n_classes, n_layers, genotype, n_big_nodes, stem_multiplier, cell_multiplier, spec_cell, slide_window)
        
        C_cur = stem_multiplier * C  # 4 * 16 = 64
        C_pp, C_p, C_cur = C_cur, C_cur, C

        self.cells = nn.ModuleList()
        for i in range(n_layers):
            if i in range(n_layers // 3):
                reduction = False
                cell = GetCell(genotype, C_pp, C_p, C_cur, reduction) if not spec_cell else Get_StageSpecified_Cell(genotype, C_pp, C_p, C_cur, False, reduction, i, n_layers)
                self.cells.append(cell)
            if i in [n_layers // 3]:
                self.bigDAG1 = SearchBigDAGPartiallyConnection(n_big_nodes, self.cells, 0, n_layers // 3, stem_multiplier*C_cur, stem_multiplier*C_cur, cell_multiplier * C_cur, window=self.window)

                reduction = True
                C_pp = C_p = 2*cell.multiplier*C_cur
                C_cur *= 2
                cell = GetCell(genotype, C_pp, C_p, C_cur, reduction) if not spec_cell else Get_StageSpecified_Cell(genotype, C_pp, C_p, C_cur, False, reduction, i, n_layers)
                self.cells.append(cell)
            if i in range(n_layers // 3 + 1, 2 * n_layers // 3):
                reduction = False
                cell = GetCell(genotype, C_pp, C_p, C_cur, reduction) if not spec_cell else Get_StageSpecified_Cell(genotype, C_pp, C_p, C_cur, False, reduction, i, n_layers)
                self.cells.append(cell)
            if i in [2 * n_layers // 3]:
                self.bigDAG2 = SearchBigDAGPartiallyConnection(n_big_nodes, self.cells, n_layers // 3 + 1, 2 * n_layers // 3, C_pp, C_p, cell_multiplier * C_cur, window=self.window)

                reduction = True
                C_pp = C_p = 2*cell.multiplier*C_cur
                C_cur *= 2
                cell = GetCell(genotype, C_pp, C_p, C_cur, reduction) if not spec_cell else Get_StageSpecified_Cell(genotype, C_pp, C_p, C_cur, False, reduction, i, n_layers)
                self.cells.append(cell)
            if i in range(2 * n_layers // 3 + 1, n_layers):
                reduction = False
                cell = GetCell(genotype, C_pp, C_p, C_cur, reduction) if not spec_cell else Get_StageSpecified_Cell(genotype, C_pp, C_p, C_cur, False, reduction, i, n_layers)
                self.cells.append(cell)

            C_pp, C_p = cell_multiplier*C_cur, cell_multiplier*C_cur

        self.bigDAG3 = SearchBigDAGPartiallyConnection(n_big_nodes, self.cells, 2 * n_layers // 3 + 1, n_layers, C_pp, C_p, cell_multiplier * C_cur, window=self.window)

        self.gap = nn.AdaptiveAvgPool2d(1)
        self.linear = nn.Linear(32 * C, n_classes)

    def forward(self, x, weights_DAG, weight_beta_DAG):
        s0 = s1 = self.stem(x)
        s0 = s1 = self.bigDAG1(s0, s1, weights_DAG[0 * self.n_big_nodes: 1 * self.n_big_nodes], weight_beta_DAG[0 * self.n_big_nodes: 1 * self.n_big_nodes])
        s0 = s1 = self.cells[1 * self.n_layers // 3](s0, s1)
        s0 = s1 = self.bigDAG2(s0, s1, weights_DAG[1 * self.n_big_nodes: 2 * self.n_big_nodes], weight_beta_DAG[0 * self.n_big_nodes: 1 * self.n_big_nodes])
        s0 = s1 = self.cells[2 * self.n_layers // 3](s0, s1)
        s0 = s1 = self.bigDAG3(s0, s1, weights_DAG[2 * self.n_big_nodes: 3 * self.n_big_nodes], weight_beta_DAG[0 * self.n_big_nodes: 1 * self.n_big_nodes])

        out = self.gap(s1)
        out = out.view(out.size(0), -1)
        logits = self.linear(out)
        return logits

class SearchStageControllerPartialConnection(SearchStageController):
    def __init__(self, input_size, C_in, C, n_classes, n_layers, criterion, genotype, stem_multiplier=4, device_ids=None, spec_cell=False, slide_window=3):
        super().__init__(input_size, C_in, C, n_classes, n_layers, criterion, genotype, stem_multiplier, device_ids, spec_cell, slide_window=slide_window)

        self.beta_DAG = nn.ParameterList()

        # 3 stages
        # initialize architecture parameter(alpha)
        for _ in range(3):
            for i in range(self.n_big_nodes):
                # sliding window
                if i + 2 < self.window:
                    self.beta_DAG.append(nn.Parameter(1e-3 * torch.randn(i + 2)))
                else:
                    self.beta_DAG.append(nn.Parameter(1e-3 * torch.randn(self.window)))

        self._betas = []
        for n, p in self.named_parameters():
            if 'beta' in n:
                self._betas.append((n, p))
        self.net = SearchStage_PartiallyConnected(input_size, C_in, C, n_classes, n_layers, genotype, self.n_big_nodes, stem_multiplier=stem_multiplier, spec_cell=spec_cell, slide_window=self.window)
    
    def forward(self, x):
        weights_DAG = [F.softmax(alpha, dim=-1) for alpha in self.alpha_DAG]
        weights_beta_DAG = [F.softmax(beta, dim=-1) for beta in self.beta_DAG]

        if len(self.device_ids) == 1:
            return self.net(x, weights_DAG, weights_beta_DAG)
        
        xs = nn.parallel.scatter(x, self.device_ids)
        wDAG_copies = broadcast_list(weights_DAG, self.device_ids)
        wbetaDAG_copies = broadcast_list(weights_beta_DAG, self.device_ids)
        replicas = nn.parallel.replicate(self.net, self.device_ids)
        outputs = nn.parallel.parallel_apply(replicas,
                                             list(zip(xs, wDAG_copies, wbetaDAG_copies)),
                                             devices=self.device_ids)
        return nn.parallel.gather(outputs, self.device_ids[0])
    
    def DAG(self):
        gene_DAG1 = gt.parse_edgeNormalization(self.alpha_DAG[0 * self.n_big_nodes: 1 * self.n_big_nodes], self.beta_DAG[0 * self.n_big_nodes: 1 * self.n_big_nodes], k=2, window=self.window)
        gene_DAG2 = gt.parse_edgeNormalization(self.alpha_DAG[1 * self.n_big_nodes: 2 * self.n_big_nodes], self.beta_DAG[1 * self.n_big_nodes: 2 * self.n_big_nodes], k=2, window=self.window)
        gene_DAG3 = gt.parse_edgeNormalization(self.alpha_DAG[2 * self.n_big_nodes: 3 * self.n_big_nodes], self.beta_DAG[2 * self.n_big_nodes: 3 * self.n_big_nodes], k=2, window=self.window)

        concat = range(self.n_big_nodes, self.n_big_nodes + 2)

        return gt.Genotype2(DAG1=gene_DAG1, DAG1_concat=concat,
                            DAG2=gene_DAG2, DAG2_concat=concat,
                            DAG3=gene_DAG3, DAG3_concat=concat)
    
    def alphas(self):
        for n, p in self._betas:
            yield p
    
    def named_alphas(self):
        for n, p in self._betas:
            yield n, p

class SearchDistributionDag(SearchStage):
    def __init__(self, C_in, C, n_classes, n_layers, genotype, n_big_nodes, stem_multiplier, slide_window=3):
        super().__init__(C_in, C, n_classes, n_layers, genotype, n_big_nodes, stem_multiplier, slide_window)

        self.bigDAG1 = SearchBigDAG_CS(n_big_nodes, self.cells, 0, n_layers // 3, 4 * C, window=self.window)
        self.bigDAG2 = SearchBigDAG_CS(n_big_nodes, self.cells, n_layers // 3 + 1, 2 * n_layers // 3, 8 * C, window=self.window)
        self.bigDAG3 = SearchBigDAG_CS(n_big_nodes, self.cells, 2 * n_layers // 3 + 1, n_layers, 16 * C, window=self.window)
    
    def forward(self, x, weights_DAG, weights_concat):
        s0 = s1 = self.stem(x)
        s0 = s1 = self.bigDAG1(s0, s1, weights_DAG[0 * self.n_big_nodes: 1 * self.n_big_nodes], weights_concat[0])
        s0 = s1 = self.cells[1 * self.n_layers // 3](s0, s1)
        s0 = s1 = self.bigDAG2(s0, s1, weights_DAG[1 * self.n_big_nodes: 2 * self.n_big_nodes], weights_concat[1])
        s0 = s1 = self.cells[2 * self.n_layers // 3](s0, s1)
        s0 = s1 = self.bigDAG3(s0, s1, weights_DAG[2 * self.n_big_nodes: 3 * self.n_big_nodes], weights_concat[2])

        out = self.gap(s1)
        out = out.view(out.size(0), -1)
        logits = self.linear(out)
        return logits

class SearchDistributionController(SearchStageController):
    def __init__(self, C_in, C, n_classes, n_layers, criterion, genotype, stem_multiplier=4, device_ids=None, slide_window=3):
        
        super().__init__(C_in, C, n_classes, n_layers, criterion, genotype, stem_multiplier=stem_multiplier, device_ids=device_ids, slide_window=slide_window)
        
        # alpha_concat = beta: for changeable stage length
        self.alpha_concat = nn.ParameterList()
        for _ in range(3):
            self.alpha_concat.append(nn.Parameter(1e-3 * torch.randn(5, 1)))
        
        self._alphas = []
        for n, p in self.named_parameters():
            if 'alpha' in n:
                self._alphas.append((n, p))
        
        self.net = SearchDistributionDag(C_in, C, n_classes, n_layers, genotype, self.n_big_nodes, stem_multiplier, slide_window)
    
    def forward(self, x):
        weights_DAG = [F.softmax(alpha, dim=-1) for alpha in self.alpha_DAG]
        weights_concat = [F.softmax(beta, dim=0) for beta in self.alpha_concat]
        if len(self.device_ids) == 1:
            return self.net(x, weights_DAG, weights_concat)

        xs = nn.parallel.scatter(x, self.device_ids)
        wDAG_copies = broadcast_list(weights_DAG, self.device_ids)
        wConcat_copies = broadcast_list(weights_concat, self.device_ids)
        replicas = nn.parallel.replicate(self.net, self.device_ids)
        outputs = nn.parallel.parallel_apply(replicas,
                                             list(zip(xs, wDAG_copies, wConcat_copies)),
                                             devices=self.device_ids)
        return nn.parallel.gather(outputs, self.device_ids[0])
    
    def print_alphas(self, logger):
        org_formatters = []
        for handler in logger.handlers:
            org_formatters.append(handler.formatter)
            handler.setFormatter(logging.Formatter("%(message)s"))
        
        logger.info("####### ALPHA #######")
        logger.info("# Alpha - DAG")
        for alpha in self.alpha_DAG:
            logger.info(F.softmax(alpha, dim=-1))
        logger.info("# Alpha - Concat")
        for beta in self.alpha_concat:
            logger.info(F.softmax(beta, dim=0))
        logger.info("#####################")

        for handler, formatter in zip(logger.handlers, org_formatters):
            handler.setFormatter(formatter)
    
    def DAG(self):
        gene_DAG1 = gt.parse(self.alpha_DAG[0 * self.n_big_nodes: 1 * self.n_big_nodes], k=2, window=self.window)
        gene_DAG2 = gt.parse(self.alpha_DAG[1 * self.n_big_nodes: 2 * self.n_big_nodes], k=2, window=self.window)
        gene_DAG3 = gt.parse(self.alpha_DAG[2 * self.n_big_nodes: 3 * self.n_big_nodes], k=2, window=self.window)

        # concat = range(2, 2+self.n_big_nodes)  # concat all intermediate nodes
        # concat = range(2+self.n_big_nodes-2, 2+self.n_big_nodes)
        concat1 = gt.parse_concat(self.alpha_concat[0])
        concat2 = gt.parse_concat(self.alpha_concat[1])
        concat3 = gt.parse_concat(self.alpha_concat[2])

        return gt.Genotype2(DAG1=gene_DAG1, DAG1_concat=concat1,
                            DAG2=gene_DAG2, DAG2_concat=concat2,
                            DAG3=gene_DAG3, DAG3_concat=concat3)

class SearchStage_FullCascade(SearchStage):
    def __init__(self, input_size, C_in, C, n_classes, n_layers, genotype, n_big_nodes, stem_multiplier=4, cell_multiplier=4, spec_cell=False):
        super().__init__(input_size, C_in, C, n_classes, n_layers, genotype, n_big_nodes, stem_multiplier, cell_multiplier, spec_cell)
        
        C_pp, C_p, C_cur = cell_multiplier * C, cell_multiplier * C, C

        self.cells = nn.ModuleList()

        for i in range(n_layers):
            if i in range(n_layers // 3):
                reduction = False
                cell = GetCell(genotype, C_pp, C_p, C_cur, reduction) if not spec_cell else Get_StageSpecified_Cell(genotype, C_pp, C_p, C_cur, False, reduction, i, n_layers)
                self.cells.append(cell)
            if i in [n_layers // 3]:
                self.bigDAG1 = SearchBigDAG_FullCascade(n_big_nodes, self.cells, 0, n_layers // 3, stem_multiplier*C_cur, stem_multiplier*C_cur, cell_multiplier * C_cur)

                reduction = True
                C_pp = C_p = 2*cell.multiplier*C_cur
                C_cur *= 2
                cell = GetCell(genotype, C_pp, C_p, C_cur, reduction) if not spec_cell else Get_StageSpecified_Cell(genotype, C_pp, C_p, C_cur, False, reduction, i, n_layers)
                self.cells.append(cell)
            if i in range(n_layers // 3 + 1, 2 * n_layers // 3):
                reduction = False
                cell = GetCell(genotype, C_pp, C_p, C_cur, reduction) if not spec_cell else Get_StageSpecified_Cell(genotype, C_pp, C_p, C_cur, False, reduction, i, n_layers)
                self.cells.append(cell)
            if i in [2 * n_layers // 3]:
                self.bigDAG2 = SearchBigDAG_FullCascade(n_big_nodes, self.cells, n_layers // 3 + 1, 2 * n_layers // 3, C_pp, C_p, cell_multiplier * C_cur)

                reduction = True
                C_pp = C_p = 2*cell.multiplier*C_cur
                C_cur *= 2
                cell = GetCell(genotype, C_pp, C_p, C_cur, reduction) if not spec_cell else Get_StageSpecified_Cell(genotype, C_pp, C_p, C_cur, False, reduction, i, n_layers)
                self.cells.append(cell)
            if i in range(2 * n_layers // 3 + 1, n_layers):
                reduction = False
                cell = GetCell(genotype, C_pp, C_p, C_cur, reduction) if not spec_cell else Get_StageSpecified_Cell(genotype, C_pp, C_p, C_cur, False, reduction, i, n_layers)
                self.cells.append(cell)

            C_pp, C_p = cell_multiplier*C_cur, cell_multiplier*C_cur


        # self.bigDAG1 = SearchBigDAG(n_big_nodes, self.cells, 0, n_layers // 3, 4 * C)
        # self.bigDAG2 = SearchBigDAG(n_big_nodes, self.cells, n_layers // 3 + 1, 2 * n_layers // 3, 8 * C)
        self.bigDAG3 = SearchBigDAG_FullCascade(n_big_nodes, self.cells, 2 * n_layers // 3 + 1, n_layers, C_pp, C_p, cell_multiplier * C_cur)
      
class SearchStageController_FullCascade(SearchStageController):
    def __init__(self, input_size, C_in, C, n_classes, n_layers, criterion, genotype, stem_multiplier=4, device_ids=None, spec_cell=False):
        super().__init__(input_size, C_in, C, n_classes, n_layers, criterion, genotype, stem_multiplier, device_ids, spec_cell)
        
        self.alpha_DAG = nn.ParameterList()
        n_ops = len(gt.PRIMITIVES2)
        # 3 stages
        # initialize architecture parameter(alpha)
        for _ in range(3):
            for i in range(self.n_big_nodes):
                self.alpha_DAG.append(nn.Parameter(1e-3 * torch.randn(i + 2, n_ops)))
        
        self._alphas = []
        for n, p in self.named_parameters():
            if 'alpha' in n:
                self._alphas.append((n, p))
                
        self.net = SearchStage_FullCascade(input_size, C_in, C, n_classes, n_layers, genotype, self.n_big_nodes, stem_multiplier=stem_multiplier, spec_cell=spec_cell)
    
    
    def DAG(self):
        gene_DAG1 = gt.parse_fullcascade(self.alpha_DAG[0 * self.n_big_nodes: 1 * self.n_big_nodes], k=2)
        gene_DAG2 = gt.parse_fullcascade(self.alpha_DAG[1 * self.n_big_nodes: 2 * self.n_big_nodes], k=2)
        gene_DAG3 = gt.parse_fullcascade(self.alpha_DAG[2 * self.n_big_nodes: 3 * self.n_big_nodes], k=2)

        concat = range(self.n_big_nodes, self.n_big_nodes + 2)

        return gt.Genotype2(DAG1=gene_DAG1, DAG1_concat=concat,
                            DAG2=gene_DAG2, DAG2_concat=concat,
                            DAG3=gene_DAG3, DAG3_concat=concat)
        
class SearchStage_Hint(SearchStage):
    """
    DAG for search
    Each edge is mixed and continuous relaxed
    """
    def __init__(self, input_size, C_in, C, n_classes, n_layers, genotype, n_big_nodes, stem_multiplier=4, cell_multiplier=4, spec_cell=False, slide_window=3):
        """
        C_in: # of input channels
        C: # of starting model channels
        n_classes: # of classes
        n_layers: # of layers
        n_big_nodes: # of intermediate n_cells  # 6
        genotype: the shape of normal cell and reduce cell
        """
        super().__init__(input_size, C_in, C, n_classes, n_layers, genotype, n_big_nodes, stem_multiplier=stem_multiplier, cell_multiplier=cell_multiplier, spec_cell=spec_cell, slide_window=slide_window)

    
    def feature_extract_forward(self, x, weights_DAG):
        s0 = s1 = self.stem(x)
        s0 = s1 = self.bigDAG1(s0, s1, weights_DAG[0 * self.n_big_nodes: 1 * self.n_big_nodes])
        stage1_features = s0 = s1 = self.cells[1 * self.n_layers // 3](s0, s1)
        
        s0 = s1 = self.bigDAG2(s0, s1, weights_DAG[1 * self.n_big_nodes: 2 * self.n_big_nodes])
        stage2_features = s0 = s1 = self.cells[2 * self.n_layers // 3](s0, s1)
        
        stage3_features = s0 = s1 = self.bigDAG3(s0, s1, weights_DAG[2 * self.n_big_nodes: 3 * self.n_big_nodes])
        
        out = self.gap(s1)
        out = out.view(out.size(0), -1)
        logits = self.linear(out)
        
        extracted_features = {
            "stage1": stage1_features,
            "stage2": stage2_features,
            "stage3": stage3_features,
            "logits": logits
        }
        return extracted_features
    
class SearchStageController_Hint(SearchStageController):
    """ SearchDAG controller supporting multi-gpu """
    def __init__(self, input_size, C_in, C, n_classes, n_layers, criterion, genotype, hint_criterion, stem_multiplier=4, device_ids=None, spec_cell=False, slide_window=3):
        super().__init__(input_size, C_in, C, n_classes, n_layers, criterion, genotype, stem_multiplier=stem_multiplier, device_ids=device_ids, spec_cell=spec_cell, slide_window=slide_window)
        self.hint_criterion = hint_criterion
        self.net = SearchStage_Hint(input_size, C_in, C, n_classes, n_layers, genotype, self.n_big_nodes, stem_multiplier=stem_multiplier, spec_cell=spec_cell, slide_window=self.window)

    def extract_features(self, x, stage=1):
        weights_DAG = [F.softmax(alpha, dim=-1) for alpha in self.alpha_DAG]

        if len(self.device_ids) == 1:
            return self.net.feature_extract_forward(x, weights_DAG)
        else:
            raise ValueError(f'不正なデバイスIDです. device_ids = "{self.device_ids}"')
        
    def freeze_stage(self, stage_ex:tuple):
        """
        特定のステージのみパラメータの更新を凍結する
        Args:
            stage_ex: 指定された番号のステージ'以外'を凍結させる
        """

        if 1 not in stage_ex:
            # ステージ１のネットワークパラメータを凍結
            for name, param in self.net.bigDAG1.named_parameters():
                param.requires_grad = False
            for name, param in self.net.cells[1 * self.n_layers // 3].named_parameters():
                param.requires_grad = False
            # ステージ１の構造パラメータを凍結
            for name, param in self.alpha_DAG[0 * self.n_big_nodes: 1 * self.n_big_nodes].named_parameters():
                param.requires_grad = False
        if 1 in stage_ex:
            for name, param in self.net.bigDAG1.named_parameters():
                param.requires_grad = True
            for name, param in self.net.cells[1 * self.n_layers // 3].named_parameters():
                param.requires_grad = True
            # ステージ２の構造パラメータを解凍
            for name, param in self.alpha_DAG[0 * self.n_big_nodes: 1 * self.n_big_nodes].named_parameters():
                param.requires_grad = True

        if 2 not in stage_ex:
            for name, param in self.net.bigDAG2.named_parameters():
                param.requires_grad = False
            for name, param in self.net.cells[2 * self.n_layers // 3].named_parameters():
                param.requires_grad = False
            # ステージ１の構造パラメータを凍結
            for name, param in self.alpha_DAG[1 * self.n_big_nodes: 2 * self.n_big_nodes].named_parameters():
                param.requires_grad = False
        if 2 in stage_ex:
            for name, param in self.net.bigDAG2.named_parameters():
                param.requires_grad = True
            for name, param in self.net.cells[2 * self.n_layers // 3].named_parameters():
                param.requires_grad = True
            # ステージ２の構造パラメータを解凍
            for name, param in self.alpha_DAG[1 * self.n_big_nodes: 2 * self.n_big_nodes].named_parameters():
                param.requires_grad = True

        if 3 not in stage_ex:
            for name, param in self.net.bigDAG3.named_parameters():
                param.requires_grad = False
            # ステージ１の構造パラメータを凍結
            for name, param in self.alpha_DAG[2 * self.n_big_nodes: 3 * self.n_big_nodes].named_parameters():
                param.requires_grad = False
        if 3 in stage_ex:
            for name, param in self.net.bigDAG3.named_parameters():
                param.requires_grad = True
            # ステージ３の構造パラメータを解凍
            for name, param in self.alpha_DAG[2 * self.n_big_nodes: 3 * self.n_big_nodes].named_parameters():
                param.requires_grad = True

        if "linear" not in stage_ex:
            for name, param in self.net.linear.named_parameters():
                param.requires_grad = False
        if "linear" in stage_ex:
            for name, param in self.net.linear.named_parameters():
                param.requires_grad = True
