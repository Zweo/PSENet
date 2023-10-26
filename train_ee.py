import os
import logging
from glob import glob

import torch
from torch.utils.data import DataLoader, Dataset

from loss import MutiLoss
from net import PSENet as MODEL


class AverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


class MData(Dataset):

    def __init__(self, dataset, mode, channel, seed):
        super().__init__()
        '''
            Adjusted to the dataset in use.
        '''
        self.data = []
        self.label = []

    def __len__(self):
        return len(self.label)

    def __getitem__(self, idx):
        '''
            returns:
                signal [B * 3 * 3000]
                label  [B * 1]

                EEG: signal[:,0,:]
                EOG: signal[:,1,:]
                ...:
        '''
        signal = self.data[idx]
        label = self.label[idx]
        if self.transformations is not None:
            signal = self.transformations(signal)
        return signal, label


def get_logger(path, name):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        '[%(asctime)s] %(name)s:%(levelname)s: %(message)s')
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.DEBUG)
    stream_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(filename=path)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)
    return logger


def init_path(args):
    name = f'{args.name}-{args.loss}'
    savepaths = [
        f'{name}', f'{name}/{args.dataset}',
        '{}/{}/{}'.format(name, args.dataset, fold)
    ]
    for pth in savepaths:
        if not os.path.exists(pth):
            os.mkdir(pth)
    save_path = savepaths[-1]
    logger = get_logger('{}/log.txt'.format(save_path), str(fold))
    logger.info('Using args :{}'.format(args))
    return logger, save_path


def test(model, target_test_loader):
    model.eval()
    test_loss = AverageMeter()
    correct = 0
    criterion = torch.nn.CrossEntropyLoss()
    len_target_dataset = len(target_test_loader.dataset)
    with torch.no_grad():
        for data, target in target_test_loader:
            data_eeg = data[:, 0, :].float().to(device).unsqueeze(1)
            data_eog = data[:, 1, :].float().to(device).unsqueeze(1)
            target = target.long().to(device)
            c1, c2, o1, o2 = model(data_eog, data_eeg)
            cout = c1 + c2
            loss = criterion(cout, target)
            test_loss.update(loss.item())
            pred = torch.max(cout, 1)[1]
            correct += torch.sum(pred == target)
    acc = 100. * correct / len_target_dataset
    return acc, test_loss.avg


def get_model(args, pth):
    start_epoch = 0
    model = MODEL().to(device)
    files = glob('{}/*.pt'.format(pth))
    if args.resume and files:
        file = files[-1]
        epoch = int(os.path.basename(file).split('-')[0]) + 1
        start_epoch = epoch
        logger.info('Load {} - Epoch:{}'.format(file, epoch - 1))
        model.load_state_dict(torch.load(file)['model'])
    return model, start_epoch


def get_dataset(args):
    dataset = {
        'train': MData(args.dataset, 'train', 'ALL', fold),
        'valid': MData(args.dataset, 'valid', 'ALL', fold),
        'test': MData(args.dataset, 'test', 'ALL', fold),
    }
    return dataset


def train(model, dataset, args, save_path, start_epoch=0):
    best_acc = 0
    file_name = ''
    train_loader = DataLoader(dataset['train'],
                              batch_size=args.batch_size,
                              shuffle=True)
    valid_loader = DataLoader(dataset['valid'], batch_size=args.batch_size)
    test_loader = DataLoader(dataset['test'], batch_size=args.batch_size)
    train_loss_clf = AverageMeter()
    criterion = MutiLoss(args.loss, args.weight, args.loss_weight)
    logger.info(f'weight:{args.weight} mode:{args.loss_weight}')
    optimizer = torch.optim.Adam(model.parameters(),
                                 lr=args.lr,
                                 eps=1e-6,
                                 weight_decay=0.0001)
    tar = None

    for epoch in range(start_epoch, args.epoch):
        model.train()
        train_loss_clf.reset()
        correct = 0

        for data, label in train_loader:
            data_eeg = data[:, 0, :].float().to(device).unsqueeze(1)
            data_eog = data[:, 1, :].float().to(device).unsqueeze(1)
            label = label.long().to(device)
            optimizer.zero_grad()
            c1, c2, o1, o2 = model(data_eog, data_eeg)
            if args.loss == 'cos':
                tar = torch.ones_like(label).to(device)
            loss = criterion(c1, label, c2, label, o1, o2, tar)
            loss.backward()
            optimizer.step()
            pred = torch.max(c1, 1)[1]
            correct += torch.sum(pred == label)
            train_loss_clf.update(loss.item())
        acc = 100. * correct / len(dataset['train'])
        info = 'Epoch: [{:2d}], total_loss: {:.4f},train-acc:[{:.4f}]'.format(
            epoch, train_loss_clf.avg, acc)
        valid_acc, valid_loss = test(model, valid_loader)
        info += f' || valid acc:[{valid_acc:.4f}], loss {valid_loss:.4f}'
        test_acc, test_loss = test(model, test_loader)
        info += f' || test acc:[{test_acc:.4f}], loss {test_loss:.4f}'

        if best_acc <= valid_acc:
            best_acc = valid_acc
            state_dict = model.state_dict()
            if file_name and os.path.exists(file_name):
                os.remove(file_name)
            file_name = '{}/{}-{:.4f}.pt'.format(save_path, epoch, test_acc)
            torch.save({'model': state_dict, 'epoch': epoch}, file_name)

        logger.info(info)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--resume', '-r', type=bool, default=True)
    parser.add_argument('--lr', type=float, default=1e-4)
    parser.add_argument('--batch_size', type=int, default=128)
    parser.add_argument('--epoch', type=int, default=100)
    parser.add_argument('--dataset', type=str, default='MASS')
    parser.add_argument('--device_id', type=int, default=0)
    parser.add_argument('--name', type=str, default='EE')
    parser.add_argument('--weight', type=list, default=[1, 1, 1])
    parser.add_argument('--start', type=int, default=21)
    parser.add_argument('--end', type=int, default=31)
    parser.add_argument('--trans', type=bool, default=False)
    parser.add_argument('--loss', type=str, default='cos')
    parser.add_argument('--loss_weight', type=str, default='auto')
    args = parser.parse_args()

    device = torch.device(
        f"cuda:{args.device_id}" if torch.cuda.is_available() else "cpu")
    for fold in range(args.start, args.end):
        logger, pth = init_path(args)
        model, start_epoch = get_model(args, pth)
        dataset = get_dataset(args)
        train(model, dataset, args, pth, start_epoch)
