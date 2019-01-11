from _context import sparse

import torch, torchvision
import numpy as np

from torchvision.transforms import ToTensor
from torch.utils.data import DataLoader

from torch.autograd import Variable

from torch import nn
import torch.nn.functional as F

from argparse import ArgumentParser

import os, tqdm

from sparse import util, NASLayer

from tqdm import trange


"""
This experiment trains a simple, fully connected two-layer MLP, using different methods of inducing sparsity, and
measures the density of the resulting weight matrices (the number of non-zero weights, divided by the total).

We aim to show that in the very low desity regime, the sparse layer is a competitive approach.

The tasks are simple classification on mnist, cifar10 and cifar100.

TODO: Test temp version.

"""

BATCH_SIZES    = [256, 128, 64, 32]
LEARNING_RATES = [0.0001, 0.0005, 0.001, 0.005, 0.01]

def getmodel(arg, insize, numcls, points):
    if arg.method == 'l1':

        one = nn.Linear(util.prod(insize), arg.hidden)
        two = nn.Linear(arg.hidden, numcls)

        model = nn.Sequential(
            util.Flatten(),
            one, nn.Sigmoid(),
            two, nn.Softmax()
        )

    elif arg.method == 'nas':

        rng = (arg.range, 1, arg.range, arg.range)

        one = NASLayer(
            in_size=insize, out_size=(arg.hidden,), k=points,
            gadditional=arg.gadditional, radditional=arg.radditional, rrange=rng, has_bias=True,
            min_sigma=arg.min_sigma
        )

        rng = (3, arg.range)

        two = NASLayer(
            in_size=(arg.hidden,), out_size=(numcls,), k=points,
            gadditional=arg.gadditional, radditional=arg.radditional, rrange=rng, has_bias=True,
            min_sigma=arg.min_sigma
        )

        model = nn.Sequential(
            one, nn.Sigmoid(),
            two, nn.Softmax()
        )
    else:
        raise Exception('Method {} not recognized'.format(arg.method))

    if arg.cuda:
        model.cuda()

    return model, one, two

def go(arg):

    lambd = 10.0 ** (-5 + arg.control)      # L1 control variable
    points = arg.hidden * (arg.control + 1) # NAS control variable

    # Grid search over batch size/learning rate
    # -- Set up model

    insize = (1, 28, 28) if arg.task == 'mnist' else (3, 32, 32)
    numcls = 100 if arg.task == 'cifar100' else 10

    ## Perform a grid search over batch size and learning rate

    print('Starting hyperparameter selection')
    bestacc = -1.0
    bestbs, bstlr = -1, -1.0

    for batch_size in BATCH_SIZES:

        # Load data with validation set
        if arg.task == 'mnist':

            NUM_TRAIN = 45000
            NUM_VAL = 5000
            total = NUM_TRAIN + NUM_VAL

            train = torchvision.datasets.MNIST(root=arg.data, train=True, download=True, transform=ToTensor())

            trainloader = DataLoader(train, batch_size=batch_size, sampler=util.ChunkSampler(0, NUM_TRAIN, total))
            testloader = DataLoader(train, batch_size=batch_size, sampler=util.ChunkSampler(NUM_TRAIN, NUM_VAL, total))
        else:
            raise Exception('Task {} not recognized'.format(arg.task))

        for lr in LEARNING_RATES:
            print('lr {}, bs {}'.format(lr, batch_size))

            model, one, two = getmodel(arg, insize, numcls, points)
            opt = torch.optim.Adam(model.parameters(), lr=lr)

            # Train for fixed number of epochs
            for e in tqdm.trange(arg.epochs):
                for input, labels in trainloader:
                    opt.zero_grad()

                    if arg.cuda:
                        input, labels = input.cuda(), labels.cuda()
                    input, labels = Variable(input), Variable(labels)

                    output = model(input)

                    loss = F.cross_entropy(output, labels)

                    if arg.method == 'l1':
                        l1 = one.weight.norm(p=1) + two.weight.norm(p=1)
                        loss = loss + lambd * l1

                    loss.backward()

                    opt.step()

            # Compute accuracy on validation set
            with torch.no_grad():
                model.train(False)

                total, correct = 0.0, 0.0
                for input, labels in testloader:
                        opt.zero_grad()

                        if arg.cuda:
                            input, labels = input.cuda(), labels.cuda()
                        input, labels = Variable(input), Variable(labels)

                        if arg.method == 'l1':
                            output = model(input)
                        else:
                            output = F.softmax(two(F.sigmoid(one(input, train=False))), dim=1)
                            # TODO: make sparse layer respond to train

                        outcls = output.argmax(dim=1)

                        total   += outcls.size(0)
                        correct += (outcls == labels).sum().item()

                print(correct, total)
                acc = correct / float(total)

                print('lr {}, bs {}: {} acc'.format(lr, batch_size, acc))

                if acc > bestacc:
                    bestbs, bestlr = batch_size, lr

    print('Hyperparameter selection finished. Best learning rate: {}. Best batch size: {}.'.format(bestlr, bestbs))

    # Repeat runs with chosen hyperparameters
    accuracies = []
    densities = []

    for r in tqdm.trange(arg.repeats):

        if (arg.task == 'mnist'):
            data = arg.data + os.sep + arg.task

            train = torchvision.datasets.MNIST(root=data, train=True, download=True, transform=ToTensor())
            trainloader = torch.utils.data.DataLoader(train, batch_size=bestbs, shuffle=True, num_workers=2)

            test = torchvision.datasets.MNIST(root=data, train=False, download=True, transform=ToTensor())
            testloader = torch.utils.data.DataLoader(test, batch_size=bestbs, shuffle=False, num_workers=2)
        else:
            raise Exception('Task {} not recognized'.format(arg.task))

        model, one, two = getmodel(arg, insize, numcls, points) # new model
        opt = torch.optim.Adam(model.parameters(), lr=lr)

        # Train for fixed number of epochs
        for e in range(arg.epochs):
            for input, labels in trainloader:
                opt.zero_grad()

                if arg.cuda:
                    input, labels = input.cuda(), labels.cuda()
                input, labels = Variable(input), Variable(labels)

                output = model(input)

                loss = F.cross_entropy(output, labels)

                if arg.method == 'l1':
                    l1 = one.weight.norm(p=1) + two.weight.norm(p=1)
                    loss = loss + lambd * l1

                loss.backward()

                opt.step()

        # Compute accuracy on test set
        with torch.no_grad():
            model.train(False)

            total, correct = 0.0, 0.0
            for input, labels in testloader:
                    opt.zero_grad()

                    if arg.cuda:
                        input, labels = input.cuda(), labels.cuda()
                    input, labels = Variable(input), Variable(labels)

                    if arg.method == 'l1':
                        output = model(input)
                    else:
                        output = F.softmax(two(F.sigmoid(one(input, train=False))), dim=1)
                        # TODO: make sparse layer respond to train

                    outcls = output.argmax(dim=1)

                    total   += outcls.size(0)
                    correct += (outcls == labels).sum().item()

            acc = correct / float(total)

        # Compute density
        total = util.prod(insize) + arg.hidden * numcls


        if arg.method == 'l1':
            density = ((one.weight != 0.0).sum() + (two.weight != 0.0).sum())/ float(total)
        elif arg.method == 'nas':
            density = (points * 2)/total
        else:
            raise Exception('Method {} not recognized'.format(arg.task))

        accuracies.append(acc)
        densities.append(density)

    print('accuracies: ', accuracies)
    print('densities: ', densities)

    # Save to CSV
    np.savetxt(
        'out.{}.{}.csv'.format(arg.method, arg.control),
        torch.cat([
                torch.tensor(accuracies, dtype=torch.float)[:, None],
                torch.tensor(densities, dtype=torch.float)[:, None]
            ], dim=1).numpy(),
    )

    print('Finished')

if __name__ == "__main__":

    parser = ArgumentParser()

    parser.add_argument("-C", "--control",
                        dest="control",
                        help="Control parameter. For l1, lambda = 10^(-5+c). For NAS, k=hidden*(c+1)",
                        default=0, type=int)

    parser.add_argument("-e", "--epochs",
                        dest="epochs",
                        help="Number of epochs",
                        default=50, type=int)

    parser.add_argument("-m", "--method",
                        dest="method",
                        help="Method to use (l1, nas) ",
                        default='l1', type=str)

    parser.add_argument("-t", "--task",
                        dest="task",
                        help="Task to use (mnist, cifar10, cifar100) ",
                        default='mnist', type=str)

    parser.add_argument("-H", "--hidden-size",
                        dest="hidden",
                        help="size of the hidden layers",
                        default=64, type=int)

    parser.add_argument("-a", "--gadditional",
                        dest="gadditional",
                        help="Number of additional points sampled globally per index-tuple (NAS)",
                        default=2, type=int)

    parser.add_argument("-A", "--radditional",
                        dest="radditional",
                        help="Number of additional points sampled locally per index-tuple (NAS)",
                        default=2, type=int)

    parser.add_argument("-R", "--range",
                        dest="range",
                        help="Range in which the local points are sampled (NAS)",
                        default=4, type=int)

    parser.add_argument("-r", "--repeats",
                        dest="repeats",
                        help="Number of times to repeat the final experiment (once the hyperparameters are chosen).",
                        default=10, type=int)

    parser.add_argument("--seed",
                        dest="seed",
                        help="Random seed",
                        default=4, type=int)

    parser.add_argument("-c", "--cuda", dest="cuda",
                        help="Whether to use cuda.",
                        action="store_true")

    parser.add_argument("-D", "--data", dest="data",
                        help="Data directory",
                        default='./data')

    parser.add_argument("-M", "--min-sigma",
                        dest="min_sigma",
                        help="Minimal sigma value",
                        default=0.01, type=float)

    args = parser.parse_args()

    print('OPTIONS', args)

    go(args)