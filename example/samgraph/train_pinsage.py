import argparse
import time
import torch
import torch as th
import torch.nn as nn
import torch.nn.functional as F
import dgl.function as fn
import torch.optim as optim
import numpy as np

import samgraph.torch as sam


"""
  We have made the following modification(or say, simplification) on PinSAGE,
  because we only want to focus on the core algorithm of PinSAGE:
    1. we modify PinSAGE to make it be able to be trained on homogenous graph.
    2. we use cross-entropy loss instead of max-margin ranking loss describe in the paper.
"""


class WeightedSAGEConv(nn.Module):
    def __init__(self, input_dims, hidden_dims, output_dims, dropout, act=F.relu):
        super().__init__()

        self.act = act
        self.Q = nn.Linear(input_dims, hidden_dims)
        self.W = nn.Linear(input_dims + hidden_dims, output_dims)
        self.reset_parameters()
        self.dropout = nn.Dropout(dropout)

    def reset_parameters(self):
        gain = nn.init.calculate_gain('relu')
        nn.init.xavier_uniform_(self.Q.weight, gain=gain)
        nn.init.xavier_uniform_(self.W.weight, gain=gain)
        nn.init.constant_(self.Q.bias, 0)
        nn.init.constant_(self.W.bias, 0)

    def forward(self, g, h, weights):
        """
        g : graph
        h : node features
        weights : scalar edge weights
        """
        h_src, h_dst = h
        with g.local_scope():
            g.srcdata['n'] = self.act(self.Q(self.dropout(h_src)))
            g.edata['w'] = weights.float()
            g.update_all(fn.u_mul_e('n', 'w', 'm'), fn.sum('m', 'n'))
            g.update_all(fn.copy_e('w', 'm'), fn.sum('m', 'ws'))
            n = g.dstdata['n']
            ws = g.dstdata['ws'].unsqueeze(1).clamp(min=1)
            z = self.act(self.W(self.dropout(torch.cat([n / ws, h_dst], 1))))
            z_norm = z.norm(2, 1, keepdim=True)
            z_norm = torch.where(
                z_norm == 0, torch.tensor(1.).to(z_norm), z_norm)
            z = z / z_norm
            return z


class PinSAGE(nn.Module):
    def __init__(self,
                 in_feats,
                 n_hidden,
                 n_classes,
                 n_layers,
                 activation,
                 dropout):
        super().__init__()
        self.n_layers = n_layers
        self.n_hidden = n_hidden
        self.n_classes = n_classes
        self.layers = nn.ModuleList()

        self.layers.append(WeightedSAGEConv(
            in_feats, n_hidden, n_hidden, dropout, activation))
        for _ in range(1, n_layers - 1):
            self.layers.append(WeightedSAGEConv(
                n_hidden, n_hidden, n_hidden, dropout, activation))
        self.layers.append(WeightedSAGEConv(
            n_hidden, n_hidden, n_classes, dropout, activation))

    def forward(self, blocks, h):
        for layer, block in zip(self.layers, blocks):
            h_dst = h[:block.number_of_nodes('DST/' + block.ntypes[0])]
            h = layer(block, (h, h_dst), block.edata['weights'])
        return h


def parse_args(default_run_config):
    argparser = argparse.ArgumentParser("PinSAGE Training")
    argparser.add_argument(
        '--arch', type=str, default=default_run_config['arch'])
    argparser.add_argument('--sample_type', type=int,
                           default=default_run_config['sample_type'])
    argparser.add_argument('--pipeline', action='store_true',
                           default=default_run_config['pipeline'])

    argparser.add_argument('--dataset-path', type=str,
                           default=default_run_config['dataset_path'])
    argparser.add_argument('--cache-policy', type=int,
                           default=default_run_config['cache_policy'])
    argparser.add_argument('--cache-percentage', type=float,
                           default=default_run_config['cache_percentage'])
    argparser.add_argument('--max-sampling-jobs', type=int,
                           default=default_run_config['max_sampling_jobs'])
    argparser.add_argument('--max-copying-jobs', type=int,
                           default=default_run_config['max_copying_jobs'])

    argparser.add_argument('--random-walk-length', type=int,
                           default=default_run_config['random_walk_length'])
    argparser.add_argument('--random-walk-restart-prob',
                           type=float, default=default_run_config['random_walk_restart_prob'])
    argparser.add_argument('--num-random-walk', type=int,
                           default=default_run_config['num_random_walk'])
    argparser.add_argument('--num-neighbor', type=int,
                           default=default_run_config['num_neighbor'])
    argparser.add_argument('--num-layer', type=int,
                           default=default_run_config['num_layer'])

    argparser.add_argument('--num-epoch', type=int,
                           default=default_run_config['num_epoch'])
    argparser.add_argument('--batch-size', type=int,
                           default=default_run_config['batch_size'])
    argparser.add_argument('--num-hidden', type=int,
                           default=default_run_config['num_hidden'])
    argparser.add_argument(
        '--lr', type=float, default=default_run_config['lr'])
    argparser.add_argument('--dropout', type=float,
                           default=default_run_config['dropout'])

    return vars(argparser.parse_args())


def get_run_config():
    default_run_config = {}
    default_run_config['arch'] = 'arch3'
    default_run_config['sample_type'] = sam.kRandomWalk
    default_run_config['pipeline'] = False  # default value must be false
    default_run_config['cache_policy'] = sam.kCacheByHeuristic
    default_run_config['cache_percentage'] = 0.25

    # default_run_config['dataset_path'] = '/graph-learning/samgraph/reddit'
    # default_run_config['dataset_path'] = '/graph-learning/samgraph/products'
    default_run_config['dataset_path'] = '/graph-learning/samgraph/papers100M'
    # default_run_config['dataset_path'] = '/graph-learning/samgraph/com-friendster'

    default_run_config['max_sampling_jobs'] = 10
    # default max_copying_jobs should be 10, but when training on com-friendster,
    # we have to set this to 1 to prevent GPU out-of-memory
    default_run_config['max_copying_jobs'] = 2

    default_run_config['random_walk_length'] = 3
    default_run_config['random_walk_restart_prob'] = 0.5
    default_run_config['num_random_walk'] = 4
    default_run_config['num_neighbor'] = 5
    default_run_config['num_layer'] = 3
    # we use the average result of 10 epochs, the first epoch is used to warm up the system
    default_run_config['num_epoch'] = 10
    default_run_config['num_hidden'] = 256
    default_run_config['batch_size'] = 8000
    default_run_config['lr'] = 0.003
    default_run_config['dropout'] = 0.5

    run_config = parse_args(default_run_config)
    print('Evaluation time: ', time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime()))
    print(*run_config.items(), sep='\n')

    run_config['arch'] = sam.meepo_archs[run_config['arch']]
    run_config['arch_type'] = run_config['arch']['arch_type']
    run_config['arch_type'] = run_config['arch']['arch_type']
    run_config['sampler_ctx'] = run_config['arch']['sampler_ctx']
    run_config['trainer_ctx'] = run_config['arch']['trainer_ctx']
    run_config['sample_type'] = sam.kRandomWalk
    # the first epoch is used to warm up the system
    run_config['num_epoch'] += 1

    # arch1 doesn't support pipelining
    if run_config['arch_type'] == sam.kArch1:
        run_config['pipeline'] = False

    return run_config


def run():
    run_config = get_run_config()

    sam.config(run_config)
    sam.config_random_walk(run_config)
    sam.init()

    sample_device = th.device('cuda:%d' % run_config['sampler_ctx'].device_id)
    train_device  = th.device('cuda:%d' % run_config['trainer_ctx'].device_id)
    print("sampling using {}".format(th.cuda.get_device_name(sample_device)))
    print("training using {}".format(th.cuda.get_device_name(train_device)))
    in_feat = sam.feat_dim()
    num_class = sam.num_class()
    num_layer = run_config['num_layer']

    model = PinSAGE(in_feat, run_config['num_hidden'], num_class,
                    num_layer, F.relu, run_config['dropout'])
    model = model.to(train_device)

    loss_fcn = nn.CrossEntropyLoss()
    loss_fcn.to(train_device)
    optimizer = optim.Adam(model.parameters(), lr=run_config['lr'])

    num_epoch = sam.num_epoch()
    num_step = sam.steps_per_epoch()

    model.train()

    epoch_sample_times  = [0 for i in range(num_epoch)]
    epoch_copy_times    = [0 for i in range(num_epoch)]
    epoch_convert_times = [0 for i in range(num_epoch)]
    epoch_train_times   = [0 for i in range(num_epoch)]
    epoch_total_times   = [0 for i in range(num_epoch)]

    # sample_times  = [0 for i in range(num_epoch * num_step)]
    # copy_times    = [0 for i in range(num_epoch * num_step)]
    # convert_times = [0 for i in range(num_epoch * num_step)]
    # train_times   = [0 for i in range(num_epoch * num_step)]
    # total_times   = [0 for i in range(num_epoch * num_step)]
    # num_nodes     = [0 for i in range(num_epoch * num_step)]
    # num_samples   = [0 for i in range(num_epoch * num_step)]

    cur_step_key = 0
    for epoch in range(num_epoch):
        for step in range(num_step):
            t0 = time.time()
            sam.trace_step_begin_now (epoch * num_step + step, sam.kL0Event_Train_Step)
            if not run_config['pipeline']:
                sam.sample_once()
            elif epoch + step == 0:
                sam.start()
            batch_key = sam.get_next_batch(epoch, step)
            t1 = time.time()
            sam.trace_step_begin_now (batch_key, sam.kL1Event_Convert)
            blocks, batch_input, batch_label = sam.get_dgl_blocks_with_weights(
                batch_key, num_layer)
            t2 = time.time()
            sam.trace_step_end_now (batch_key, sam.kL1Event_Convert)
            sam.trace_step_begin_now (batch_key, sam.kL1Event_Train)
            batch_pred = model(blocks, batch_input)
            loss = loss_fcn(batch_pred, batch_label)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            sam.trace_step_end_now   (batch_key, sam.kL1Event_Train)
            t3 = time.time()
            sam.trace_step_end_now (epoch * num_step + step, sam.kL0Event_Train_Step)

            # sample_time = sam.get_log_step_value(epoch, step, sam.kLogL1SampleTime)
            # copy_time = sam.get_log_step_value(epoch, step, sam.kLogL1CopyTime)
            convert_time = t2 - t1
            train_time = t3 - t2
            total_time = t3 - t0

            # num_node = sam.get_log_step_value(epoch, step, sam.kLogL1NumNode)
            # num_sample = sam.get_log_step_value(epoch, step, sam.kLogL1NumSample)

            sam.log_step(epoch, step, sam.kLogL1TrainTime,   train_time)
            sam.log_step(epoch, step, sam.kLogL1ConvertTime, convert_time)
            sam.log_epoch_add(epoch, sam.kLogEpochConvertTime, convert_time)
            sam.log_epoch_add(epoch, sam.kLogEpochTrainTime,   train_time)
            sam.log_epoch_add(epoch, sam.kLogEpochTotalTime,   total_time)

            # sample_times  [cur_step_key] = sample_time
            # copy_times    [cur_step_key] = copy_time
            # convert_times [cur_step_key] = convert_time
            # train_times   [cur_step_key] = train_time
            # total_times   [cur_step_key] = total_time

            # num_nodes     [cur_step_key] = num_node
            # num_samples   [cur_step_key] = num_sample

            # print('Epoch {:05d} | Step {:05d} | Nodes {:.0f} | Samples {:.0f} | Time {:.4f} secs | Sample Time {:.4f} secs | Copy Time {:.4f} secs |  Train Time {:.4f} secs (Convert Time {:.4f} secs) | Loss {:.4f} '.format(
            #     epoch, step, num_node, num_sample, total_time, 
            #         sample_time, copy_time, train_time, convert_time, loss
            # ))

            # sam.report_step_average(epoch, step)
            sam.report_step(epoch, step)
            cur_step_key += 1

        # sam.report_epoch_average(epoch)

        epoch_sample_times  [epoch] =  sam.get_log_epoch_value(epoch, sam.kLogEpochSampleTime)
        epoch_copy_times    [epoch] =  sam.get_log_epoch_value(epoch, sam.kLogEpochCopyTime)
        epoch_convert_times [epoch] =  sam.get_log_epoch_value(epoch, sam.kLogEpochConvertTime)
        epoch_train_times   [epoch] =  sam.get_log_epoch_value(epoch, sam.kLogEpochTrainTime)
        epoch_total_times   [epoch] =  sam.get_log_epoch_value(epoch, sam.kLogEpochTotalTime)

    print('Avg Epoch Time {:.4f} | Sample Time {:.4f} | Copy Time {:.4f} | Convert Time {:.4f} | Train Time {:.4f}'.format(
        np.mean(epoch_total_times[1:]),  np.mean(epoch_sample_times[1:]),  np.mean(epoch_copy_times[1:]), np.mean(epoch_convert_times[1:]), np.mean(epoch_train_times[1:])))

    sam.report_node_access()
    sam.dump_trace()
    sam.shutdown()


if __name__ == '__main__':
    run()
