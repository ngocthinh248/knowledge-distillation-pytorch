"""Train the model"""

import argparse
import logging
import os
import time
import math
import random
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.optim.lr_scheduler import StepLR
from torch.autograd import Variable
from tqdm import tqdm

import utils
import model.net as net
import model.data_loader as data_loader
import model.resnet as resnet
import model.wrn as wrn
import model.densenet as densenet
from evaluate import evaluate, evaluate_kd

parser = argparse.ArgumentParser()
# parser.add_argument('--data_dir', default='data/64x64_SIGNS', help="Directory for the dataset")
parser.add_argument('--model_dir', default='experiments/base_model',
                    help="Directory containing params.json")
parser.add_argument('--restore_file', default=None,
                    help="Optional, name of the file in --model_dir \
                    containing weights to reload before training")  # 'best' or 'train'


def train(model, optimizer, loss_fn, dataloader, metrics, params):
    """Train the model on `num_steps` batches

    Args:
        model: (torch.nn.Module) the neural network
        optimizer: (torch.optim) optimizer for parameters of model
        loss_fn: 
        dataloader: 
        metrics: (dict) 
        params: (Params) hyperparameters
    """

    # set model to training mode
    model.train()

    # summary for current training loop and a running average object for loss
    summ = []
    loss_avg = utils.RunningAverage()

    # Use tqdm for progress bar
    with tqdm(total=len(dataloader)) as t:
        for i, (train_batch, labels_batch) in enumerate(dataloader):
            # move to GPU if available
            if params.cuda:
                train_batch, labels_batch = train_batch.cuda(async=True), \
                                            labels_batch.cuda(async=True)
            # convert to torch Variables
            train_batch, labels_batch = Variable(train_batch), Variable(labels_batch)

            # compute model output and loss
            output_batch = model(train_batch)
            loss = loss_fn(output_batch, labels_batch)

            # clear previous gradients, compute gradients of all variables wrt loss
            optimizer.zero_grad()
            loss.backward()

            # performs updates using calculated gradients
            optimizer.step()

            # Evaluate summaries only once in a while
            if i % params.save_summary_steps == 0:
                # extract data from torch Variable, move to cpu, convert to numpy arrays
                output_batch = output_batch.data.cpu().numpy()
                labels_batch = labels_batch.data.cpu().numpy()

                # compute all metrics on this batch
                summary_batch = {metric:metrics[metric](output_batch, labels_batch)
                                 for metric in metrics}
                summary_batch['loss'] = loss.data[0]
                summ.append(summary_batch)

            # update the average loss
            loss_avg.update(loss.data[0])

            t.set_postfix(loss='{:05.3f}'.format(loss_avg()))
            t.update()

    # compute mean of all metrics in summary
    metrics_mean = {metric:np.mean([x[metric] for x in summ]) for metric in summ[0]}
    metrics_string = " ; ".join("{}: {:05.3f}".format(k, v) for k, v in metrics_mean.items())
    logging.info("- Train metrics: " + metrics_string)


def train_and_evaluate(model, train_dataloader, val_dataloader, optimizer,
                       loss_fn, metrics, params, model_dir, restore_file=None):
    """Train the model and evaluate every epoch.

    Args:
        model: (torch.nn.Module) the neural network
        params: (Params) hyperparameters
        model_dir: (string) directory containing config, weights and log
        restore_file: (string) optional- name of file to restore from (without its extension .pth.tar)
    """
    # reload weights from restore_file if specified
    if restore_file is not None:
        restore_path = os.path.join(args.model_dir, args.restore_file + '.pth.tar')
        logging.info("Restoring parameters from {}".format(restore_path))
        utils.load_checkpoint(restore_path, model, optimizer)

    best_val_acc = 0.0

    # learning rate schedulers for different models:
    if params.model_version == "resnet18":
        scheduler = StepLR(optimizer, step_size=100, gamma=0.1)
    elif params.model_version == "wrn":
        scheduler = StepLR(optimizer, step_size=60, gamma=0.2)
    elif params.model_version == "cnn":
        scheduler = StepLR(optimizer, step_size=100, gamma=0.2)

    for epoch in range(params.num_epochs):
     
        scheduler.step()
     
        # Run one epoch
        logging.info("Epoch {}/{}".format(epoch + 1, params.num_epochs))

        # compute number of batches in one epoch (one full pass over the training set)
        train(model, optimizer, loss_fn, train_dataloader, metrics, params)

        # Evaluate for one epoch on validation set
        val_metrics = evaluate(model, loss_fn, val_dataloader, metrics, params)        

        val_acc = val_metrics['accuracy']
        is_best = val_acc>=best_val_acc

        # Save weights
        utils.save_checkpoint({'epoch': epoch + 1,
                               'state_dict': model.state_dict(),
                               'optim_dict' : optimizer.state_dict()},
                               is_best=is_best,
                               checkpoint=model_dir)

        # If best_eval, best_save_path
        if is_best:
            logging.info("- Found new best accuracy")
            best_val_acc = val_acc

            # Save best val metrics in a json file in the model directory
            best_json_path = os.path.join(model_dir, "metrics_val_best_weights.json")
            utils.save_dict_to_json(val_metrics, best_json_path)

        # Save latest val metrics in a json file in the model directory
        last_json_path = os.path.join(model_dir, "metrics_val_last_weights.json")
        utils.save_dict_to_json(val_metrics, last_json_path)

        # if epoch == params.num_epochs - 1:
        #     train_metrics = evaluate(model, loss_fn, train_dataloader, metrics, params)
        #     last_json_path = os.path.join(model_dir, "metrics_train_last_weights.json")
        #     utils.save_dict_to_json(train_metrics, last_json_path)


# Helper: get [batch_idx, teacher_outputs] list by running teacher model once
def fetch_teacher_outputs(teacher_model, dataloader, params):
    # set teacher_model to evaluation mode
    teacher_model.eval()
    teacher_outputs = []
    for i, (data_batch, labels_batch) in enumerate(dataloader):
        if params.cuda:
            data_batch, labels_batch = data_batch.cuda(async=True), \
                                        labels_batch.cuda(async=True)
        data_batch, labels_batch = Variable(data_batch), Variable(labels_batch)

        output_teacher_batch = teacher_model(data_batch).data.cpu().numpy()
        teacher_outputs.append(output_teacher_batch)

    return teacher_outputs

# Defining train_kd & train_and_evaluate_kd functions

def train_kd(model, teacher_ensemble_outputs, optimizer, loss_fn_kd, dataloader, metrics, params):
    """Train the model on `num_steps` batches

    Args:
        model: (torch.nn.Module) the neural network
        optimizer: (torch.optim) optimizer for parameters of model
        loss_fn_kd: 
        dataloader: 
        metrics: (dict) 
        params: (Params) hyperparameters
    """

    # set model to training mode
    model.train()
    # teacher_model.eval()

    # summary for current training loop and a running average object for loss
    summ = []
    loss_avg = utils.RunningAverage()

    # Use tqdm for progress bar
    with tqdm(total=len(dataloader)) as t:
        for i, (train_batch, labels_batch) in enumerate(dataloader):
            # move to GPU if available
            if params.cuda:
                train_batch, labels_batch = train_batch.cuda(async=True), \
                                            labels_batch.cuda(async=True)
            # convert to torch Variables
            train_batch, labels_batch = Variable(train_batch), Variable(labels_batch)

            # compute model output, fetch teacher output, and compute KD loss
            output_batch = model(train_batch)

            # get one batch output from teacher_outputs list
            loss = 0.
            for teacher_idx in range(3):
                output_teacher_batch = torch.from_numpy(teacher_ensemble_outputs[teacher_idx][i])
                if params.cuda:
                    output_teacher_batch = output_teacher_batch.cuda(async=True)
                output_teacher_batch = Variable(output_teacher_batch, requires_grad=False)

                loss += loss_fn_kd(output_batch, labels_batch, output_teacher_batch, params)
            
            loss = loss / 3.
            # clear previous gradients, compute gradients of all variables wrt loss
            optimizer.zero_grad()
            loss.backward()

            # performs updates using calculated gradients
            optimizer.step()

            # Evaluate summaries only once in a while
            if i % params.save_summary_steps == 0:
                # extract data from torch Variable, move to cpu, convert to numpy arrays
                output_batch = output_batch.data.cpu().numpy()
                labels_batch = labels_batch.data.cpu().numpy()

                # compute all metrics on this batch
                summary_batch = {metric:metrics[metric](output_batch, labels_batch)
                                 for metric in metrics}
                summary_batch['loss'] = loss.data[0]
                summ.append(summary_batch)

            # update the average loss
            loss_avg.update(loss.data[0])

            t.set_postfix(loss='{:05.3f}'.format(loss_avg()))
            t.update()

    # compute mean of all metrics in summary
    metrics_mean = {metric:np.mean([x[metric] for x in summ]) for metric in summ[0]}
    metrics_string = " ; ".join("{}: {:05.3f}".format(k, v) for k, v in metrics_mean.items())
    logging.info("- Train metrics: " + metrics_string)


def train_and_evaluate_kd(model, teacher_ensemble, train_dataloader, val_dataloader, optimizer,
                       loss_fn_kd, metrics, params, model_dir, restore_file=None):
    """Train the model and evaluate every epoch.

    Args:
        model: (torch.nn.Module) the neural network
        params: (Params) hyperparameters
        model_dir: (string) directory containing config, weights and log
        restore_file: (string) optional- file to restore (without its extension .pth.tar)
    """
    # reload weights from restore_file if specified
    if restore_file is not None:
        restore_path = os.path.join(args.model_dir, args.restore_file + '.pth.tar')
        logging.info("Restoring parameters from {}".format(restore_path))
        utils.load_checkpoint(restore_path, model, optimizer)

    best_val_acc = 0.0

    # Compute teacher outputs using teacher_model under eval() mode
    loading_start = time.time()
    teacher_ensemble_outputs = []
    for teacher_name in ["resnet18", "wrn", "densenet"]:
        teacher_model = teacher_ensemble[teacher_name]
        teacher_model.eval()
        teacher_outputs = fetch_teacher_outputs(teacher_model, train_dataloader, params)
        teacher_ensemble_outputs.append(teacher_outputs)

    elapsed_time = math.ceil(time.time() - loading_start)
    logging.info("- Finished computing teacher outputs after {} secs..".format(elapsed_time))

    # learning rate schedulers for different models:
    if params.model_version == "resnet18_distill":
        scheduler = StepLR(optimizer, step_size=100, gamma=0.1)
    elif params.model_version == "cnn_distill":
        scheduler = StepLR(optimizer, step_size=100, gamma=0.2)

    for epoch in range(params.num_epochs):

        scheduler.step()

        # Run one epoch
        logging.info("Epoch {}/{}".format(epoch + 1, params.num_epochs))

        # compute number of batches in one epoch (one full pass over the training set)
        train_kd(model, teacher_ensemble_outputs, optimizer, loss_fn_kd, train_dataloader,
                 metrics, params)

        # Evaluate for one epoch on validation set
        val_metrics = evaluate_kd(model, val_dataloader, metrics, params)

        val_acc = val_metrics['accuracy']
        is_best = val_acc>=best_val_acc

        # Save weights
        utils.save_checkpoint({'epoch': epoch + 1,
                               'state_dict': model.state_dict(),
                               'optim_dict' : optimizer.state_dict()},
                               is_best=is_best,
                               checkpoint=model_dir)

        # If best_eval, best_save_path
        if is_best:
            logging.info("- Found new best accuracy")
            best_val_acc = val_acc

            # Save best val metrics in a json file in the model directory
            best_json_path = os.path.join(model_dir, "metrics_val_best_weights.json")
            utils.save_dict_to_json(val_metrics, best_json_path)

        # Save latest val metrics in a json file in the model directory
        last_json_path = os.path.join(model_dir, "metrics_val_last_weights.json")
        utils.save_dict_to_json(val_metrics, last_json_path)

        # if epoch == params.num_epochs - 1:
        #     train_metrics = evaluate_kd(model, train_dataloader, metrics, params)
        #     last_json_path = os.path.join(model_dir, "metrics_train_last_weights.json")
        #     utils.save_dict_to_json(train_metrics, last_json_path)


if __name__ == '__main__':

    # Load the parameters from json file
    args = parser.parse_args()
    json_path = os.path.join(args.model_dir, 'params.json')
    assert os.path.isfile(json_path), "No json configuration file found at {}".format(json_path)
    params = utils.Params(json_path)

    # use GPU if available
    params.cuda = torch.cuda.is_available()

    # Set the random seed for reproducible experiments
    random.seed(230)
    torch.manual_seed(230)
    if params.cuda: torch.cuda.manual_seed(230)

    # Set the logger
    utils.set_logger(os.path.join(args.model_dir, 'train.log'))

    # Create the input data pipeline
    logging.info("Loading the datasets...")

    # fetch dataloaders, considering full-set vs. sub-set scenarios
    if params.subset_percent < 1.0:
        train_dl = data_loader.fetch_subset_dataloader('train', params)
    else:
        train_dl = data_loader.fetch_dataloader('train', params)
    
    dev_dl = data_loader.fetch_dataloader('dev', params)

    logging.info("- done.")

    """Based on the model_version, determine model/optimizer and KD training mode
       WideResNet and DenseNet were trained on multi-GPU; need to specify a dummy
       nn.DataParallel module to correctly load the model parameters
    """
    if "distill" in params.model_version:

        if params.model_version == "cnn_distill":
            model = net.Net(params).cuda() if params.cuda else net.Net(params)
            optimizer = optim.Adam(model.parameters(), lr=params.learning_rate)
            loss_fn_kd = net.loss_fn_kd
            metrics = net.metrics
        
        elif params.model_version == 'resnet18_distill':
            model = resnet.ResNet18().cuda() if params.cuda else resnet.ResNet18()
            optimizer = optim.SGD(model.parameters(), lr=params.learning_rate,
                                  momentum=0.9, weight_decay=5e-4)
            # fetch loss function and metrics
            loss_fn_kd = net.loss_fn_kd
            metrics = resnet.metrics

        # use resnet/wrn/densenet for knowledge distillation during training
        if params.teacher == "resnet18":
            teacher_model = resnet.ResNet18()
            teacher_checkpoint = 'experiments/base_resnet18/best.pth.tar'
            teacher_model = teacher_model.cuda() if params.cuda else teacher_model

        elif params.teacher == "wrn":
            teacher_model = wrn.WideResNet(depth=28, num_classes=10, widen_factor=10,
                                           dropRate=0.3)
            teacher_checkpoint = 'experiments/base_wrn/best.pth.tar'
            teacher_model = nn.DataParallel(teacher_model).cuda()

        elif params.teacher == "densenet":
            teacher_model = densenet.DenseNet(depth=100, growthRate=12)
            teacher_checkpoint = 'experiments/base_densenet/best.pth.tar'
            teacher_model = nn.DataParallel(teacher_model).cuda()

        elif params.teacher == "ensemble":
            teacher_ensemble = {}
            teacher_ensemble["resnet18"] = resnet.ResNet18()
            teacher_checkpoint = 'experiments/base_resnet18/best.pth.tar'
            teacher_ensemble["resnet18"] = teacher_ensemble["resnet18"].cuda() if params.cuda \
                                           else teacher_ensemble["resnet18"]
            utils.load_checkpoint(teacher_checkpoint, teacher_ensemble["resnet18"])
            
            teacher_ensemble["wrn"] = wrn.WideResNet(depth=28, num_classes=10, widen_factor=10,
                                                   dropRate=0.3)
            teacher_checkpoint = 'experiments/base_wrn/best.pth.tar'
            teacher_ensemble["wrn"] = nn.DataParallel(teacher_ensemble["wrn"]).cuda()
            utils.load_checkpoint(teacher_checkpoint, teacher_ensemble["wrn"])
            
            teacher_ensemble["densenet"] = densenet.DenseNet(depth=100, growthRate=12)
            teacher_checkpoint = 'experiments/base_densenet/best.pth.tar'
            teacher_ensemble["densenet"] = nn.DataParallel(teacher_ensemble["densenet"]).cuda()
            utils.load_checkpoint(teacher_checkpoint, teacher_ensemble["densenet"])

        # Train the model with KD
        logging.info("Starting training for {} epoch(s)".format(params.num_epochs))
        logging.info("First, loading the teacher model and computing its outputs...")
        train_and_evaluate_kd(model, teacher_ensemble, train_dl, dev_dl, optimizer, loss_fn_kd,
                              metrics, params, args.model_dir, args.restore_file)

    # non-KD mode: regular training (with CrossEntropy) 
    else:
        if params.model_version == "cnn":
            model = net.Net(params).cuda() if params.cuda else net.Net(params)
            optimizer = optim.Adam(model.parameters(), lr=params.learning_rate)
            # fetch loss function and metrics
            loss_fn = net.loss_fn
            metrics = net.metrics

        elif params.model_version == "wrn":
            model = wrn.wrn(depth=28, num_classes=10, widen_factor=10, dropRate=0.3)
            model = model.cuda() if params.cuda else model
            optimizer = optim.SGD(model.parameters(), lr=params.learning_rate,
                                  momentum=0.9, weight_decay=5e-4)
            # fetch loss function and metrics
            loss_fn = wrn.loss_fn
            metrics = wrn.metrics

        elif params.model_version == "resnet18":
            model = resnet.ResNet18().cuda() if params.cuda else resnet.ResNet18()
            optimizer = optim.SGD(model.parameters(), lr=params.learning_rate,
                                  momentum=0.9, weight_decay=5e-4)
            # fetch loss function and metrics
            loss_fn = resnet.loss_fn
            metrics = resnet.metrics

        # Train the model
        logging.info("Starting training for {} epoch(s)".format(params.num_epochs))
        train_and_evaluate(model, train_dl, dev_dl, optimizer, loss_fn, metrics, params,
                           args.model_dir, args.restore_file)

