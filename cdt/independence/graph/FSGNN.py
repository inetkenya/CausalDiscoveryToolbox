"""Feature selection model with generative models.

Algorithm between SAM and CGNN
Author : Diviyan Kalainathan & Olivier Goudet

.. MIT License
..
.. Copyright (c) 2018 Diviyan Kalainathan
..
.. Permission is hereby granted, free of charge, to any person obtaining a copy
.. of this software and associated documentation files (the "Software"), to deal
.. in the Software without restriction, including without limitation the rights
.. to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
.. copies of the Software, and to permit persons to whom the Software is
.. furnished to do so, subject to the following conditions:
..
.. The above copyright notice and this permission notice shall be included in all
.. copies or substantial portions of the Software.
..
.. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
.. IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
.. FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
.. AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
.. LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
.. OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
.. SOFTWARE.
"""
import torch as th
import numpy as np
from torch.utils import data
from sklearn.preprocessing import scale
from tqdm import trange
from .model import FeatureSelectionModel
from ...utils.Settings import SETTINGS
from ...utils.loss import MMDloss


class Dataset(data.Dataset):
    """Characterizes a dataset for PyTorch

    Args:
        features (pandas.DataFrame): Features given as input
        target (pandas.DataFrame): Feature considered as target
    """
    def __init__(self, features, target, device):
        'Initialization'
        self.target = th.Tensor(scale(target.values)).to(device)
        self.features = th.Tensor(scale(features.values)).to(device)

    def __len__(self):
        'Denotes the total number of samples'
        return len(self.features)

    def __featurelen__(self):
        "Number of features of the dataset"
        return self.features.shape[1]

    def __getitem__(self, index):
        'Generates one sample of data'
        # Load data and get label
        return self.features[index], self.target[index]


class FSGNN_model(th.nn.Module):
    def __init__(self, sizes, dropout=0., activation_function=th.nn.ReLU):
        super(FSGNN_model, self).__init__()
        layers = []

        for i, j in zip(sizes[:-2], sizes[1:-1]):
            layers.append(th.nn.Linear(i, j))
            if dropout != 0.:
                layers.append(th.nn.Dropout(p=dropout))
            layers.append(activation_function())

        layers.append(th.nn.Linear(sizes[-2], sizes[-1]))
        self.layers = th.nn.Sequential(*layers)

    def forward(self, x):
        self.layers(x)

    def train(self, dataset, lr=0.01, l1=0.1, batch_size=-1,
              train_epochs=1000, test_epochs=1000, device=None,
              verbose=None, dataloader_workers=0):
        device, verbose = SETTINGS.get_default(('device', device), ('verbose', verbose))
        optim = th.optim.Adam(self.parameters(), lr=lr)
        output = th.zeros(dataset.__featurelen__()).to(device)

        if batch_size == -1:
            batch_size = dataset.__len__()
        criterion = MMDloss(input_size=batch_size).to(device)
        # Printout value
        noise = th.randn(batch_size, 1).to(device)
        data_iterator = th.utils.data.DataLoader(dataset, batch_size=batch_size,
                                                 shuffle=True, drop_last=True,
                                                 num_workers=dataloader_workers)
        # TRAIN
        with trange(train_epochs + test_epochs, disable=not verbose) as t:
            for epoch in t:
                for i, (batch, target) in enumerate(data_iterator):
                    optim.zero_grad()
                    noise.normal_()
                    gen = self.layers(th.cat([batch, noise], 1))
                    # print(gen)
                    loss = criterion(gen, target) + l1*(self.layers[0].weight.abs().sum() + self.layers[2].weight.abs().sum())
                    # Train the discriminator
                    if not epoch % 100 and i == 0:
                        t.set_postfix(epoch=epoch, loss=loss.item())
                    if epoch >= train_epochs:
                        output.add_(self.layers[0].weight.data[:, :-1].sum(dim=0))
                    loss.backward()
                    optim.step()

        return list(output.div_(test_epochs).div_(dataset.__len__()//batch_size).cpu().numpy())


class FSGNN(FeatureSelectionModel):
    """Feature Selection using MMD and Generative Neural Networks.

    Args:
        nh (int): number of hidden units
        dropout (float): probability of dropout (between 0 and 1)
        activation_function (torch.nn.Module): activation function of the NN
        lr (float): learning rate of Adam
        l1 (float): L1 penalization coefficient
        batch_size (int): batch size, defaults to full-batch
        train_epochs (int): number of train epochs
        test_epochs (int): number of test epochs
        verbose (bool): verbosity (defaults to ``cdt.SETTINGS.verbose``)
        nruns (int): number of bootstrap runs
        dataloader_workers (int): how many subprocesses to use for data
           loading. 0 means that the data will be loaded in the main
           process. (default: 0)
           
   Example:
       >>> from cdt.independence.graph import FSGNN
       >>> from sklearn.datasets import load_boston
       >>> boston = load_boston()
       >>> df_features = pd.DataFrame(boston['data'])
       >>> df_target = pd.DataFrame(boston['target'])
       >>> obj = FSGNN()
       >>> output = obj.predict(df_features, df_target)
    """

    def __init__(self, nh=20, dropout=0., activation_function=th.nn.ReLU,
                 lr=0.01, l1=0.1,  batch_size=-1, train_epochs=1000,
                 test_epochs=1000, verbose=None, nruns=3,
                 dataloader_workers=0):
        """Init the model."""
        super(FSGNN, self).__init__()
        self.nh = nh
        self.dropout = dropout
        self.activation_function = activation_function
        self.lr = lr
        self.l1 = l1
        self.batch_size = batch_size
        self.train_epochs = train_epochs
        self.test_epochs = test_epochs
        self.verbose = SETTINGS.get_default(verbose=verbose)
        self.nruns = nruns
        self.dataloader_workers = dataloader_workers

    def predict_features(self, df_features, df_target, datasetclass=Dataset,
                         device=None, idx=0):
        """For one variable, predict its neighbours.

        Args:
            df_features (pandas.DataFrame): Features to select
            df_target (pandas.Series): Target variable to predict
            datasetclass (torch.utils.data.Dataset): Class to override for
               custom loading of data.
            idx (int): (optional) for printing purposes
            device (str): cuda or cpu device (defaults to
               ``cdt.SETTINGS.default_device``)

        Returns:
            list: scores of each feature relatively to the target

        """
        device = SETTINGS.get_default(device=device)
        dataset = datasetclass(df_features, df_target, device)
        out = []
        for i in range(self.nruns):
            model = FSGNN_model([dataset.__featurelen__() + 1, self.nh, 1],
                                activation_function=self.activation_function,
                                dropout=self.dropout).to(device)

            out.append(model.train(dataset, lr=0.01, l1=0.1,
                                   batch_size=self.batch_size,
                                   train_epochs=self.train_epochs,
                                   test_epochs=self.test_epochs,
                                   device=device, verbose=self.verbose,
                                   dataloader_workers=self.dataloader_workers))
        return list(np.mean(np.array(out), axis=0))
