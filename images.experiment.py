import hyper, gaussian_in, util, time, pretrain, os, math, sys, PIL
import torch, random
from torch.autograd import Variable
from torch import nn, optim
from torch.nn import Parameter
from tqdm import trange, tqdm
from tensorboardX import SummaryWriter
from util import Lambda, Debug
from torch.utils.serialization import load_lua

from torch.utils.data import TensorDataset, DataLoader

from torchsample.metrics import CategoricalAccuracy

import torch.optim as optim

import torchvision
import torchvision.transforms as transforms
from torchvision.datasets import MNIST, CIFAR10, CIFAR100

from util import od, prod, logit

from argparse import ArgumentParser

import logging

import matplotlib as mpl
mpl.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

LOG = logging.getLogger('ash')
LOG.setLevel(logging.INFO)
fh = logging.FileHandler('ash.log')
fh.setLevel(logging.INFO)
LOG.addHandler(fh)

def inv(i, max):
    sc = (i/max) * 0.999 + 0.0005
    return logit(sc)

def sigmoid(x):
    if type(x) == float:
        return 1 / (1 + math.exp(-x))
    return 1 / (1 + torch.exp(-x))

class ImageLayer(gaussian_in.HyperLayer):
    """
    Simple hyperlayer for the 1D MNIST experiment

    NB: k is the number of tuples _per hidden node_.
    """

    def __init__(self, in_size, out_size, k, adaptive=True, additional=0, sigma_scale=0.1, num_values=-1, min_sigma=0.0, pre=0, subsample=None):

        out_indices = torch.LongTensor(list(np.ndindex(out_size)))

        out_indices = out_indices.unsqueeze(1).expand(prod(out_size), k, len(out_size))
        out_indices = out_indices.contiguous().view(prod(out_size) * k, len(out_size))

        print(out_indices.size()[0], ' index tuples')

        super().__init__(in_rank=3, out_size=out_size, out_indices=out_indices, additional=additional, subsample=subsample)

        assert(len(in_size) == 3)

        self.in_size = in_size
        self.k = k
        self.sigma_scale = sigma_scale
        self.num_values = num_values
        self.min_sigma = min_sigma
        self.out_size = out_size
        self.adaptive = adaptive
        self.pre = pre

        # outsize = k * prod(out_size) * 5

        # one-hot matrix for the inputs to the hypernetwork
        one_hots = torch.zeros(out_indices.size()[0], sum(out_size) + k)
        for r in range(out_indices.size()[0]):

            min = 0
            for i in range(len(out_size)):
                one_hots[r, min + int(out_indices[r, i])] = 1
                min += out_size[i]

            one_hots[r, min + r % k] = 1
            # print(out_indices[r, :], out_size)
            # print(one_hots[r, :])

        # convert out_indices to float values that return the correct indices when sigmoided.
        # out_indices = inv(out_indices, torch.FloatTensor(out_size).unsqueeze(0).expand_as(out_indices))
        self.register_buffer('one_hots', one_hots)

        if self.adaptive:
            activation = nn.ReLU()

            assert(pre > 0)

            c , w, h = in_size
            hid = math.ceil(w/4) * math.ceil(h/4) * 8

            self.preprocess = nn.Sequential(
                nn.MaxPool2d(kernel_size=4),
                # util.Debug(lambda x: print(x.size())),
                nn.Conv2d(c, 8, kernel_size=5, padding=2),
                activation,
                nn.Conv2d(8, 8, kernel_size=5, padding=2),
                activation,
                # util.Debug(lambda x : print(x.size())),
                util.Flatten(),
                nn.Linear(hid, pre),
                nn.Sigmoid()
            )

            self.source = nn.Sequential(
                nn.Linear(pre + sum(out_size) + k, 4), # input + output index (one hots) + k (one hot)
                # activation,
                # nn.Linear(hidden, hidden),
                # activation,
                # nn.Linear(hidden, 5),
            )

            self.sigmas = Parameter(torch.randn((1, self.k * prod(out_size), 1)))

        else:
            self.nas = Parameter(torch.randn((self.k * prod(out_size), 5)))

        self.bias = Parameter(torch.zeros(*out_size))

        if num_values > 0:
            self.values = Parameter(torch.randn((num_values,)))

    def hyper(self, input):
        """
        Evaluates hypernetwork.
        """

        b, c, w, h = input.size()
        # l, d  = self.out_indices.size() # prod(out_shape) * k
        l, dh = self.one_hots.size()

        # outs = Variable(self.out_indices.unsqueeze(0).expand(b, l, d))
        hots = Variable(self.one_hots.unsqueeze(0).expand(b, l, dh))

        if self.adaptive:

            input = self.preprocess(input)

            b, d = input.size()
            assert(d == self.pre)

            input = input.unsqueeze(1).expand(b, l, d)
            input = torch.cat([input, hots], dim=2)

            input = input.view(b*l, -1)

            res = self.source(input).view(b, l , 4)

            ss = self.sigmas.expand(b, l, 1)

            res = torch.cat([res[:, :, :-1], ss, res[:, :, -1:]], dim=2)

        else:
            res = self.nas.unsqueeze(0).expand(b, l, 5)

        means, sigmas, values = self.split_out(res, self.in_size)

        sigmas = sigmas * self.sigma_scale + self.min_sigma

        if self.num_values > 0:
            mult = l // self.num_values

            values = self.values.unsqueeze(0).expand(mult, self.num_values)
            values = values.contiguous().view(-1)[:l]

            values = values.unsqueeze(0).expand(b, l)

        self.last_values = values.data

        return means, sigmas, values, self.bias

    def plot(self, images):
        perrow = 5

        num, c, w, h = images.size()

        rows = int(math.ceil(num/perrow))

        means, sigmas, values, _ = self.hyper(images)

        images = images.data

        plt.figure(figsize=(perrow * 3, rows*3))

        for i in range(num):

            ax = plt.subplot(rows, perrow, i+1)

            im = np.transpose(images[i, :, :, :].cpu().numpy(), (1, 2, 0))
            im = np.squeeze(im)

            ax.imshow(im, interpolation='nearest', extent=(-0.5, w-0.5, -0.5, h-0.5), cmap='gray_r')

            util.plot(means[i, :, 1:].unsqueeze(0), sigmas[i, :, 1:].unsqueeze(0), values[i, :].unsqueeze(0), axes=ax)

        plt.gcf()

PLOT = True
COLUMN = 13

def go(batch=64, epochs=350, k=3, additional=64, modelname='baseline', cuda=False,
       seed=1, lr=0.001, subsample=None, num_values=-1, min_sigma=0.0,
       tb_dir=None, data='./data', hidden=32, task='mnist', final=False, pre=3):

    FT = torch.cuda.FloatTensor if cuda else torch.FloatTensor

    torch.manual_seed(seed)

    w = SummaryWriter(log_dir=tb_dir)

    normalize = transforms.Compose([transforms.ToTensor()])

    if(task=='mnist'):
        data = data + os.sep + task

        if final:
            train = torchvision.datasets.MNIST(root=data, train=True, download=True, transform=normalize)
            trainloader = torch.utils.data.DataLoader(train, batch_size=batch, shuffle=True, num_workers=2)

            test = torchvision.datasets.MNIST(root=data, train=False, download=True, transform=normalize)
            testloader = torch.utils.data.DataLoader(test, batch_size=batch, shuffle=False, num_workers=2)

        else:
            NUM_TRAIN = 45000
            NUM_VAL = 5000

            train = torchvision.datasets.MNIST(root=data, train=True, download=True, transform=normalize)

            trainloader = DataLoader(train, batch_size=batch, sampler=util.ChunkSampler(NUM_TRAIN, 0))
            testloader = DataLoader(train, batch_size=batch, sampler=util.ChunkSampler(NUM_VAL, NUM_TRAIN))

        shape = (1, 28, 28)
        num_classes = 10

    elif (task == 'image-folder-bw'):

        if final:
            raise Exception('not implemented yet')
        else:
            NUM_TRAIN = 45000
            NUM_VAL = 5000

            train = torchvision.datasets.ImageFolder(root=data, transform=normalize)

            trainloader = DataLoader(train, batch_size=batch, sampler=util.ChunkSampler(NUM_TRAIN, 0))
            testloader = DataLoader(train, batch_size=batch, sampler=util.ChunkSampler(NUM_VAL, NUM_TRAIN))

        shape = (3, 100, 100)
        num_classes = 10

    elif(task=='cifar10'):
        data = data + os.sep + task

        if final:
            train = torchvision.datasets.CIFAR10(root=data, train=True, download=True, transform=normalize)
            trainloader = torch.utils.data.DataLoader(train, batch_size=batch, shuffle=True, num_workers=2)
            test = torchvision.datasets.CIFAR10(root=data, train=False, download=True, transform=normalize)
            testloader = torch.utils.data.DataLoader(test, batch_size=batch, shuffle=False, num_workers=2)

        else:
            NUM_TRAIN = 45000
            NUM_VAL = 5000

            train = torchvision.datasets.CIFAR10(root=data, train=True, download=True, transform=normalize)

            trainloader = DataLoader(train, batch_size=batch, sampler=util.ChunkSampler(NUM_TRAIN, 0))
            testloader = DataLoader(train, batch_size=batch, sampler=util.ChunkSampler(NUM_VAL, NUM_TRAIN))


        shape = (3, 32, 32)
        num_classes = 10

    elif(task=='cifar100'):

        data = data + os.sep + task

        if final:
            train = torchvision.datasets.CIFAR100(root=data, train=True, download=True, transform=normalize)
            trainloader = torch.utils.data.DataLoader(train, batch_size=batch, shuffle=True, num_workers=2)
            test = torchvision.datasets.CIFAR100(root=data, train=False, download=True, transform=normalize)
            testloader = torch.utils.data.DataLoader(test, batch_size=batch, shuffle=False, num_workers=2)

        else:
            NUM_TRAIN = 45000
            NUM_VAL = 5000

            train = torchvision.datasets.CIFAR100(root=data, train=True, download=True, transform=normalize)

            trainloader = DataLoader(train, batch_size=batch, sampler=util.ChunkSampler(NUM_TRAIN, 0))
            testloader = DataLoader(train, batch_size=batch, sampler=util.ChunkSampler(NUM_VAL, NUM_TRAIN))

        shape = (3, 32, 32)
        num_classes = 100

    else:
        raise Exception('Task name {} not recognized'.format(task))

    activation = nn.ReLU()

    hyperlayer = None

    if modelname == 'baseline':

        model = nn.Sequential(
            util.Flatten(),
            nn.Linear(prod(shape), hidden),
            activation,
            nn.Linear(hidden, num_classes),
            nn.Softmax())

    elif modelname == 'baseline-conv':

        fin = (shape[1]//16) * (shape[2]//16) * 128

        model = nn.Sequential(
            nn.Conv2d(shape[0], 16, kernel_size=5, padding=2), activation,
            nn.MaxPool2d(kernel_size=2),
            nn.Conv2d(16, 32, kernel_size=5, padding=2), activation,
            nn.MaxPool2d(kernel_size=2),
            nn.Conv2d(32, 64, kernel_size=5, padding=2), activation,
            nn.MaxPool2d(kernel_size=2),
            nn.Conv2d(64, 128, kernel_size=5, padding=2), activation,
            nn.MaxPool2d(kernel_size=2),
            util.Flatten(),
            nn.Dropout(0.5),
            nn.Linear(fin, num_classes),
            nn.Softmax())

    elif modelname == 'ash':

        hyperlayer = ImageLayer(shape, out_size=(hidden,), k=k, adaptive=True, additional=additional, num_values=num_values,
                                min_sigma=min_sigma, subsample=subsample, pre=pre)

        model = nn.Sequential(
            hyperlayer,
            nn.Linear(hidden, hidden),
            activation,
            nn.Linear(hidden, num_classes),
            nn.Softmax())

    elif modelname == 'nas':

        hyperlayer = ImageLayer(shape, out_size=(hidden,), k=k,  adaptive=False, additional=additional, num_values=num_values,
                                min_sigma=min_sigma, subsample=subsample)

        model = nn.Sequential(
            hyperlayer,
            nn.Linear(hidden, hidden),
            activation,
            nn.Linear(hidden, num_classes),
            nn.Softmax())

    elif modelname == 'ash1':

        hyperlayer = ImageLayer(shape, out_size=(num_classes,), k=k, adaptive=True, additional=additional, num_values=num_values,
                                min_sigma=min_sigma, subsample=subsample, pre=pre)

        model = nn.Sequential(
            hyperlayer,
            nn.Softmax())

    elif modelname == 'nas1':

        hyperlayer = ImageLayer(shape, out_size=(num_classes,), k=k,  adaptive=False, additional=additional, num_values=num_values,
                                min_sigma=min_sigma, subsample=subsample)

        model = nn.Sequential(
            hyperlayer,
            nn.Softmax())

    elif modelname == 'nas-conv':
        ch = 3

        hyperlayer = ImageLayer(shape, out_size=(ch, 4, 4), k=k, adaptive=False, additional=additional, num_values=num_values,
                                min_sigma=min_sigma, subsample=subsample)

        model = nn.Sequential(
            hyperlayer,
            activation,
            nn.Conv2d(ch, 128, kernel_size=5, padding=2), activation,
            nn.MaxPool2d(kernel_size=2),
            util.Flatten(),
            nn.Linear(128 * 2 * 2, num_classes),
            nn.Softmax())

    elif modelname == 'ash-conv':
        ch = 3

        hyperlayer = ImageLayer(shape, out_size=(ch, 4, 4), k=k, adaptive=True, additional=additional, num_values=num_values,
                                min_sigma=min_sigma, subsample=subsample)

        model = nn.Sequential(
            hyperlayer,
            activation,
            nn.Conv2d(ch, 128, kernel_size=5, padding=2), activation,
            nn.MaxPool2d(kernel_size=2),
            util.Flatten(),
            nn.Linear(128 * 2 * 2, num_classes),
            nn.Softmax())

    else:
        raise Exception('Model name {} not recognized'.format(modelname))

    if cuda:
        model.cuda()
        if hyperlayer is not None:
            hyperlayer.apply(lambda t: t.cuda())

    optimizer = optim.Adam(model.parameters(), lr=lr)

    xent = nn.CrossEntropyLoss()
    acc = CategoricalAccuracy()

    step = 0

    sigs, vals = [], []

    util.makedirs('./mnist/')

    for epoch in range(epochs):

        for i, data in tqdm(enumerate(trainloader, 0)):

            # get the inputs
            inputs, labels = data

            if cuda:
                inputs, labels = inputs.cuda(), labels.cuda()

            # wrap them in Variables
            inputs, labels = Variable(inputs), Variable(labels)

            optimizer.zero_grad()

            outputs = model(inputs)

            loss = xent(outputs, labels)

            t0 = time.time()
            loss.backward()  # compute the gradients
            logging.info('backward: {} seconds'.format(time.time() - t0))

            # print(hyperlayer.values, hyperlayer.values.grad)

            optimizer.step()
            w.add_scalar('mnist/train-loss', loss.data[0], step)

            step += inputs.size()[0]

            if PLOT and i == 0 and hyperlayer is not None:

                sigmas = list(hyperlayer.last_sigmas[0, :])
                values = list(hyperlayer.last_values[0, :])

                sigs.append(sigmas)
                vals.append(values)

                ax = plt.figure().add_subplot(111)

                for j, (s, v) in enumerate(zip(sigs, vals)):
                    ax.scatter([j] * len(s), s, c=v, linewidth=0,  alpha=0.2, cmap='RdYlBu', vmin=-1.0, vmax=1.0)

                ax.set_aspect('auto')
                plt.ylim(ymin=0)
                util.clean()

                plt.savefig('sigmas.pdf')
                plt.savefig('sigmas.png')

                hyperlayer.plot(inputs[:10, ...])
                plt.savefig('mnist/attention.{:03}.pdf'.format(epoch))

        total = 0.0
        num = 0

        for i, data in enumerate(testloader, 0):

            # get the inputs
            inputs, labels = data

            if cuda:
                inputs, labels = inputs.cuda(), labels.cuda()

            # wrap them in Variables
            inputs, labels = Variable(inputs), Variable(labels)

            outputs = model(inputs)

            total += acc(outputs, labels)
            num += 1

        accuracy = total / num

        w.add_scalar('mnist1d/per-epoch-test-acc', accuracy, epoch)
        print('EPOCH {}: {} accuracy '.format(epoch, accuracy))

    LOG.info('Finished Training.')

if __name__ == "__main__":

    ## Parse the command line options
    parser = ArgumentParser()

    parser.add_argument("-e", "--epochs",
                        dest="epochs",
                        help="Number of epochs over thegenerated data.",
                        default=350, type=int)

    parser.add_argument("-m", "--model",
                        dest="model",
                        help="Which model to train.",
                        default='baseline')

    parser.add_argument("-b", "--batch-size",
                        dest="batch_size",
                        help="The batch size.",
                        default=64, type=int)

    parser.add_argument("-k", "--num-points",
                        dest="k",
                        help="Number of index tuples in the decoder layer",
                        default=3, type=int)

    parser.add_argument("-a", "--additional",
                        dest="additional",
                        help="Number of additional points sampled",
                        default=64, type=int)

    parser.add_argument("-c", "--cuda", dest="cuda",
                        help="Whether to use cuda.",
                        action="store_true")

    parser.add_argument("-D", "--data", dest="data",
                        help="Data directory",
                        default='./data/')

    parser.add_argument("-l", "--learn-rate",
                        dest="lr",
                        help="Learning rate",
                        default=0.001, type=float)

    parser.add_argument("-S", "--subsample",
                        dest="subsample",
                        help="Sample a subset of the indices to estimate gradients for",
                        default=None, type=float)

    parser.add_argument("-F", "--num-values", dest="num_values",
                        help="How many fixed values to allow the network",
                        default=-1, type=int)

    parser.add_argument("-M", "--min-sigma",
                        dest="min_sigma",
                        help="Minimum value of sigma.",
                        default=0.0, type=float)

    parser.add_argument("-T", "--tb_dir", dest="tb_dir",
                        help="Data directory",
                        default=None)

    parser.add_argument("-t", "--task", dest="task",
                        help="Task (mnist, cifar10, cifar100)",
                        default='mnist')

    parser.add_argument("-f", "--final", dest="final",
                        help="Whether to run on the real test set.",
                        action="store_true")

    parser.add_argument("-H", "--hidden", dest="hidden",
                        help="Size of the hidden layer.",
                        default=32, type=int)

    parser.add_argument("-L", "--data-location", dest="data_location",
                        help="Location of the data (if not downloadable).",
                        default='/Users/Peter/Desktop/mnist-cluttered/mnist/')


    parser.add_argument("-p", "--pre", dest="pre",
                        help="Size of the preprocessed input representation.",
                        default=32, type=int)

    options = parser.parse_args()

    print('OPTIONS ', options)
    LOG.info('OPTIONS ' + str(options))

    go(epochs=options.epochs, batch=options.batch_size, k=options.k,
        additional=options.additional, modelname=options.model, cuda=options.cuda,
        lr=options.lr, subsample=options.subsample,
        num_values=options.num_values, min_sigma=options.min_sigma,
        tb_dir=options.tb_dir, data=options.data, task=options.task,
        final=options.final, hidden=options.hidden, pre=options.pre)
