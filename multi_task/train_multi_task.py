import sys
import torch
import argparse
import json
import datetime, time, os
from timeit import default_timer as timer

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from torch.utils import data
import torchvision
import types

from tqdm import tqdm
from tensorboardX import SummaryWriter

#from models.gradient_scaler import MinNormElement
import losses
import datasets
import metrics
import model_selector
from min_norm_solvers import MinNormSolver, gradient_normalizers

NUM_EPOCHS = 100

# @click.command()
# @click.option('--param_file', default='./sample.json', help='JSON parameters file')
parser = argparse.ArgumentParser(description='Multi-Objective Optimization: CelebA')
parser.add_argument('--param_file', default='./sample.json', type=str, help='JSON parameters file')
parser.add_argument('--gpu', default= '1', type=str, help='Which gpu want to use.')

opt = parser.parse_args()
os.environ["CUDA_VISIBLE_DEVICES"] = opt.gpu

def train_multi_task():
    with open('configs.json') as config_params:
        configs = json.load(config_params)

    with open(opt.param_file) as json_params:
        params = json.load(json_params)


    exp_identifier = []
    log_dir_name = ''
    for (key, val) in params.items():
        if 'tasks' in key:
            continue
        elif 'scales' not in key:
            log_dir_name += str(val) + '_'
        else:
            pass
        exp_identifier+= ['{}={}'.format(key,val)]


    exp_identifier = '|'.join(exp_identifier)
    params['exp_id'] = exp_identifier
    writer = SummaryWriter(log_dir='runs/{}_{}'.format(log_dir_name[:-1], datetime.datetime.now().strftime("%I_%M%p_on_%B_%d,%Y")))

    train_loader, train_dst, val_loader, val_dst = datasets.get_dataset(params, configs)
    loss_fn = losses.get_loss(params)
    metric = metrics.get_metrics(params)

    model = model_selector.get_model(params)
    model_params = []
    for m in model:
        model_params += model[m].parameters()

    if 'RMSprop' in params['optimizer']:
        optimizer = torch.optim.RMSprop(model_params, lr=params['lr'])
    elif 'Adam' in params['optimizer']:
        optimizer = torch.optim.Adam(model_params, lr=params['lr'])
    elif 'SGD' in params['optimizer']:
        optimizer = torch.optim.SGD(model_params, lr=params['lr'], momentum=0.9)

    tasks = params['tasks']
    all_tasks = configs[params['dataset']]['all_tasks']
    print('Starting training with parameters \n \t{} \n'.format(str(params)))

    if 'mgda' in params['algorithm']:
        approximate_norm_solution = params['use_approximation']
        if approximate_norm_solution:
            print('Using approximate min-norm solver')
        else:
            print('Using full solver')
    n_iter = 0
    loss_init = {}
    for epoch in tqdm(range(NUM_EPOCHS)):
        start = timer()
        print('Epoch {} Started'.format(epoch))
        if (epoch+1) % 10 == 0:
            # Every 50 epoch, half the LR
            for param_group in optimizer.param_groups:
                param_group['lr'] *= 0.85
            print('Half the learning rate{}'.format(n_iter))

        for m in model:
            model[m].train()

        for batch in tqdm(train_loader):
            n_iter += 1
            # First member is always images
            images = batch[0]
            images = Variable(images.cuda())

            labels = {}
            # Read all targets of all tasks
            for i, t in enumerate(all_tasks):
                if t not in tasks:
                    continue
                labels[t] = batch[i+1]
                labels[t] = Variable(labels[t].cuda())

            # Scaling the loss functions based on the algorithm choice
            loss_data = {}
            grads = {}
            scale = {}
            mask = None
            masks = {}
            if 'mgda' in params['algorithm']:
                # Will use our MGDA_UB if approximate_norm_solution is True. Otherwise, will use MGDA

                if approximate_norm_solution:
                    optimizer.zero_grad()
                    # First compute representations (z)
                    with torch.no_grad():
                        images_volatile = Variable(images.data)
                        rep, mask = model['rep'](images_volatile, mask)
                    # As an approximate solution we only need gradients for input
                    if isinstance(rep, list):
                        # This is a hack to handle psp-net
                        rep = rep[0]
                        rep_variable = [Variable(rep.data.clone(), requires_grad=True)]
                        list_rep = True
                    else:
                        rep_variable = Variable(rep.data.clone(), requires_grad=True)
                        list_rep = False
                    
                    # Compute gradients of each loss function wrt z
                    for t in tasks:
                        optimizer.zero_grad()
                        out_t, masks[t] = model[t](rep_variable, None)
                        loss = loss_fn[t](out_t, labels[t])
                        loss_data[t] = loss.data.item()
                        loss.backward()
                        grads[t] = []
                        if list_rep:
                            grads[t].append(Variable(rep_variable[0].grad.data.clone(), requires_grad=False))
                            rep_variable[0].grad.data.zero_()
                        else:
                            grads[t].append(Variable(rep_variable.grad.data.clone(), requires_grad=False))
                            rep_variable.grad.data.zero_()
                else:
                    # This is MGDA
                    for t in tasks:
                        # Comptue gradients of each loss function wrt parameters
                        optimizer.zero_grad()
                        rep, mask = model['rep'](images, mask)
                        out_t, masks[t] = model[t](rep, None)
                        loss = loss_fn[t](out_t, labels[t])
                        loss_data[t] = loss.data.item()
                        loss.backward()
                        grads[t] = []
                        for param in model['rep'].parameters():
                            if param.grad is not None:
                                grads[t].append(Variable(param.grad.data.clone(), requires_grad=False))

                # Normalize all gradients, this is optional and not included in the paper. See the notebook for details
                gn = gradient_normalizers(grads, loss_data, params['normalization_type'])
                for t in tasks:
                    for gr_i in range(len(grads[t])):
                        grads[t][gr_i] = grads[t][gr_i] / gn[t]

                # Frank-Wolfe iteration to compute scales.
                sol, min_norm = MinNormSolver.find_min_norm_element([grads[t] for t in tasks])
                for i, t in enumerate(tasks):
                    scale[t] = float(sol[i])
            else:
                for t in tasks:
                    masks[t] = None
                    scale[t] = float(params['scales'][t])

            # Scaled back-propagation
            optimizer.zero_grad()
            rep, _ = model['rep'](images, mask)
            for i, t in enumerate(tasks):
                out_t, _ = model[t](rep, masks[t])
                loss_t = loss_fn[t](out_t, labels[t])
                loss_data[t] = loss_t.data.item()
                if i > 0:
                    loss = loss + scale[t]*loss_t
                else:
                    loss = scale[t]*loss_t
            loss.backward()
            optimizer.step()

            writer.add_scalar('training_loss', loss.data.item(), n_iter)
            for t in tasks:
                writer.add_scalar('training_loss_{}'.format(t), loss_data[t], n_iter)

        for m in model:
            model[m].eval()

        tot_loss = {}
        tot_loss['all'] = 0.0
        met = {}
        for t in tasks:
            tot_loss[t] = 0.0
            met[t] = 0.0

        num_val_batches = 0
        for batch_val in val_loader:
            with torch.no_grad():
                val_images = Variable(batch_val[0].cuda())
            labels_val = {}

            for i, t in enumerate(all_tasks):
                if t not in tasks:
                    continue
                labels_val[t] = batch_val[i+1]
                with torch.no_grad():
                    labels_val[t] = Variable(labels_val[t].cuda())

            val_rep, _ = model['rep'](val_images, None)
            for t in tasks:
                out_t_val, _ = model[t](val_rep, None)
                loss_t = loss_fn[t](out_t_val, labels_val[t])
                tot_loss['all'] += loss_t.data.item()
                tot_loss[t] += loss_t.data.item()
                metric[t].update(out_t_val, labels_val[t])
            num_val_batches+=1

        for t in tasks:
            writer.add_scalar('validation_loss_{}'.format(t), tot_loss[t]/num_val_batches, n_iter)
            metric_results = metric[t].get_result()
            for metric_key in metric_results:
                writer.add_scalar('metric_{}_{}'.format(metric_key, t), metric_results[metric_key], n_iter)
            metric[t].reset()
        writer.add_scalar('validation_loss', tot_loss['all']/len(val_dst), n_iter)

        if epoch % 3 == 0:
            # Save after every 3 epoch
            state = {'epoch': epoch+1,
                    'model_rep': model['rep'].state_dict(),
                    'optimizer_state' : optimizer.state_dict()}
            for t in tasks:
                key_name = 'model_{}'.format(t)
                state[key_name] = model[t].state_dict()
            if not os.path.exists('saved_models'):
                os.mkdir('saved_models')
            torch.save(state, "saved_models/{}_{}_model.pkl".format(log_dir_name[:-1], epoch+1))
        end = timer()
        print('Epoch ended in {}s'.format(end - start))


if __name__ == '__main__':
    #param_file = './sample.json'
    train_multi_task()
