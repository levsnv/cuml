# Copyright (c) 2019-2021, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Data generators for cuML benchmarks

The main entry point for consumers is gen_data, which
wraps the underlying data generators.

Notes when writing new generators:

Each generator is a function that accepts:
 * n_samples (set to 0 for 'default')
 * n_features (set to 0 for 'default')
 * random_state
 * (and optional generator-specific parameters)

The function should return a 2-tuple (X, y), where X is a Pandas
dataframe and y is a Pandas series. If the generator does not produce
labels, it can return (X, None)

A set of helper functions (convert_*) can convert these to alternative
formats. Future revisions may support generating cudf dataframes or
GPU arrays directly instead.

"""

import cudf
import gzip
import functools
from enum import Enum
import numpy as np
import os
import pandas as pd
import pickle

import cuml.datasets
import sklearn.model_selection
from sklearn.datasets import load_svmlight_file, fetch_covtype

from urllib.request import urlretrieve
from cuml.common import input_utils
from numba import cuda

from cuml.common.import_utils import has_scipy


def _gen_data_regression(n_samples, n_features, random_state=42):
    """Wrapper for sklearn make_regression"""
    if n_samples == 0:
        n_samples = int(1e6)
    if n_features == 0:
        n_features = 100
    X_arr, y_arr = cuml.datasets.make_regression(
        n_samples=n_samples, n_features=n_features, random_state=random_state)
    return cudf.DataFrame(X_arr), cudf.Series(y_arr)


def _gen_data_blobs(n_samples, n_features, random_state=42, centers=None):
    """Wrapper for sklearn make_blobs"""
    if n_samples == 0:
        n_samples = int(1e6)
    if n_features == 0:
        n_samples = 100
    X_arr, y_arr = cuml.datasets.make_blobs(
        n_samples=n_samples, n_features=n_features, centers=centers,
        random_state=random_state)
    return (
        cudf.DataFrame(X_arr.astype(np.float32)),
        cudf.Series(y_arr.astype(np.float32)),
    )


def _gen_data_zeros(n_samples, n_features, random_state=42):
    """Dummy generator for use in testing - returns all 0s"""
    return (
        cudf.DataFrame(np.zeros((n_samples, n_features), dtype=np.float32)),
        cudf.Series(np.zeros(n_samples, dtype=np.float32)),
    )


def _gen_data_classification(
    n_samples, n_features, random_state=42, n_classes=2
):
    """Wrapper for sklearn make_blobs"""
    if n_samples == 0:
        n_samples = int(1e6)
    if n_features == 0:
        n_samples = 100

    X_arr, y_arr = cuml.datasets.make_classification(
        n_samples=n_samples, n_features=n_features, n_classes=n_classes,
        random_state=random_state)

    return (
        cudf.DataFrame(X_arr.astype(np.float32)),
        cudf.Series(y_arr.astype(np.float32)),
    )


def _unpickle_and_crop_df(df_name, load_df, n_samples, n_features):
    """Generic function to exexute loading a dataset, then crop it"""
    pickle_url = os.path.join(DATASETS_DIRECTORY,
                              "%s-%d-samples.pkl" % (df_name, n_samples))
    print(pickle_url)
    if os.path.exists(pickle_url):
        X, y = pickle.load(open(pickle_url, "rb"))
    else:
        X, y = load_df(n_samples)
        pickle.dump((X, y), open(pickle_url, "wb"), protocol=4)
    
    if not n_samples:
        n_samples = X.shape[0]
    if not n_features:
        n_features = X.shape[1]
    if n_features > X.shape[1]:
        raise ValueError(
            "%s dataset has only %d features, cannot support %d"
            % (df_name, X.shape[1], n_features)
        )
    if n_samples > X.shape[0]:
        raise ValueError(
            "%s dataset has only %d rows, cannot support %d"
            % (df_name, X.shape[0], n_samples)
        )
    return X.iloc[:n_samples, :n_features], y.iloc[:n_samples]


def show_progress(block_num, block_size, total_size):
    global pbar
    if pbar is None:
        pbar = tqdm.tqdm(total=total_size / 1024, unit='kB')

    downloaded = block_num * block_size
    if downloaded < total_size:
        pbar.update(block_size / 1024)
    else:
        pbar.close()
        pbar = None


class LearningTask(Enum):
    REGRESSION = 1
    CLASSIFICATION = 2
    MULTICLASS_CLASSIFICATION = 3


class Data:  # pylint: disable=too-few-public-methods,too-many-arguments
    def __init__(self, X_train, X_test, y_train, y_test, learning_task, qid_train=None,
                 qid_test=None):
        self.X_train = X_train
        self.X_test = X_test
        self.y_train = y_train
        self.y_test = y_test
        self.learning_task = learning_task
        # For ranking task
        self.qid_train = qid_train
        self.qid_test = qid_test


# Default location to cache datasets
DATASETS_DIRECTORY = '.'


def _download_and_cache(url):
    compressed_filepath = os.path.join(DATASETS_DIRECTORY, os.path.basename(url))
    if not os.path.isfile(compressed_filepath):
        urlretrieve(url, compressed_filepath)

    decompressed_filepath=os.path.splitext(compressed_filepath)[0]
    if not os.path.isfile(decompressed_filepath):
        cf = gzip.GzipFile(compressed_filepath)
        with open(decompressed_filepath, 'wb') as df:
            df.write(cf.read())
    return decompressed_filepath


def _gen_data_bosch(n_samples=0, n_features=0, random_state=42):
    """Wrapper returning Bosch dataset in Pandas format"""
    dataset_name = "Bosch"
    def load_df(n_samples):
        print("kaggle competitions download -c bosch-production-line-performance -f " +
                  filename + " -p " + DATASETS_DIRECTORY)
        filename = "train_numeric.csv.zip"
        local_url = os.path.join(DATASETS_DIRECTORY, filename)
        os.system("kaggle competitions download -c bosch-production-line-performance -f " +
                  filename + " -p " + DATASETS_DIRECTORY)
        kwargs = {'nrows': n_samples} if n_samples else {}
        X = pd.read_csv(local_url, index_col=0, compression='zip', dtype=np.float32,
                        **kwargs)
        y = X.iloc[:, -1].to_numpy(dtype=np.float32)
        X.drop(X.columns[-1], axis=1, inplace=True)
        X = X.to_numpy(dtype=np.float32)
        return X, y
    return _unpickle_and_crop_df(dataset_name, load_df, n_samples, n_features)


def _gen_data_covtype(n_samples=0, n_features=0, random_state=42):
    """Wrapper returning covtype in Pandas format"""
    def load_df(n_samples):
        return fetch_covtype(return_X_y=True)  # pylint: disable=unexpected-keyword-arg
    return _unpickle_and_crop_df("covtype", load_df, n_samples, n_features)


def _gen_data_epsilon(n_samples=0, n_features=0, random_state=42):
    """Wrapper returning epsilon dataset in Pandas format"""
    def load_df(n_samples):
        url_train = 'https://www.csie.ntu.edu.tw/~cjlin/libsvmtools' \
                    '/datasets/binary/epsilon_normalized.bz2'
        train_uncompressed = _download_and_cache(url_train)
        print('loading ', train_uncompressed)
        X, y = load_svmlight_file(train_uncompressed, dtype=np.float32)
        if not n_samples or n_samples > y_train.size:
            url_test = 'https://www.csie.ntu.edu.tw/~cjlin/libsvmtools' \
                       '/datasets/binary/epsilon_normalized.t.bz2'
            test_uncompressed = _download_and_cache(url_test)
            print('loading ', test_uncompressed)
            X_test, y_test = load_svmlight_file(test_uncompressed,
                                                dtype=np.float32)
            X = np.vstack(X, X_test)
            y = np.append(y, y_test)

        X = X.toarray()
        y[y <= 0] = 0

        return X, y
    return _unpickle_and_crop_df("epsilon", load_df, n_samples, n_features)


def _gen_data_year(n_samples=0, n_features=0, random_state=42):
    """Wrapper returning Year dataset in Pandas format"""
    def load_df(n_samples):
      url = 'https://archive.ics.uci.edu/ml/machine-learning-databases/00203/YearPredictionMSD.txt' \
            '.zip'
      uncompressed = _download_and_cache(url)
      kwargs = {'nrows': n_samples} if n_samples else {}
      year = pd.read_csv(uncompressed, header=None, **kwargs)
      X = year.iloc[:, 1:].to_numpy(dtype=np.float32)
      y = year.iloc[:, 0].to_numpy(dtype=np.float32)

      return X, y
    return _unpickle_and_crop_df("Year", load_df, n_samples, n_features)


def _gen_data_higgs(n_samples=0, n_features=0, random_state=42):
    """Wrapper returning Higgs in cudf format"""
    def load_higgs(n_samples):
        """Returns the Higgs Boson dataset as an X, y tuple of dataframes."""
        higgs_url = 'https://archive.ics.uci.edu/ml/machine-learning-databases' \
            '/00280/HIGGS.csv.gz'  # noqa
        decompressed_filepath = _download_and_cache(higgs_url)

        col_names = ['label'] + [
            "col-{}".format(i) for i in range(2, 30)
        ]  # Assign column names
        dtypes_ls = [np.int32] + [
            np.float32 for _ in range(2, 30)
        ]  # Assign dtypes to each column
        kwargs = {'nrows': n_samples} if n_samples else {}
        data_df = pd.read_csv(
            decompressed_filepath, names=col_names, **kwargs,
            dtype={k: v for k, v in zip(col_names, dtypes_ls)}
        )
        X_df = data_df[data_df.columns.difference(['label'])]
        y_df = data_df['label']
        return cudf.DataFrame.from_pandas(X_df), cudf.Series.from_pandas(y_df)
    return _unpickle_and_crop_df("Higgs", load_higgs, n_samples, n_features)


def _convert_to_numpy(data):
    """Returns tuple data with all elements converted to numpy ndarrays"""
    if data is None:
        return None
    elif isinstance(data, tuple):
        return tuple([_convert_to_numpy(d) for d in data])
    elif isinstance(data, np.ndarray):
        return data
    elif isinstance(data, cudf.DataFrame):
        return data.as_matrix()
    elif isinstance(data, cudf.Series):
        return data.to_array()
    elif isinstance(data, (pd.DataFrame, pd.Series)):
        return data.to_numpy()
    else:
        raise Exception("Unsupported type %s" % str(type(data)))


def _convert_to_cudf(data):
    if data is None:
        return None
    elif isinstance(data, tuple):
        return tuple([_convert_to_cudf(d) for d in data])
    elif isinstance(data, (cudf.DataFrame, cudf.Series)):
        return data
    elif isinstance(data, pd.DataFrame):
        return cudf.DataFrame.from_pandas(data)
    elif isinstance(data, pd.Series):
        return cudf.Series.from_pandas(data)
    else:
        raise Exception("Unsupported type %s" % str(type(data)))


def _convert_to_pandas(data):
    if data is None:
        return None
    elif isinstance(data, tuple):
        return tuple([_convert_to_pandas(d) for d in data])
    elif isinstance(data, (pd.DataFrame, pd.Series)):
        return data
    elif isinstance(data, (cudf.DataFrame, cudf.Series)):
        return data.to_pandas()
    else:
        raise Exception("Unsupported type %s" % str(type(data)))


def _convert_to_gpuarray(data, order='F'):
    if data is None:
        return None
    elif isinstance(data, tuple):
        return tuple([_convert_to_gpuarray(d, order=order) for d in data])
    elif isinstance(data, pd.DataFrame):
        return _convert_to_gpuarray(cudf.DataFrame.from_pandas(data),
                                    order=order)
    elif isinstance(data, pd.Series):
        gs = cudf.Series.from_pandas(data)
        return cuda.as_cuda_array(gs)
    else:
        return input_utils.input_to_cuml_array(
            data, order=order)[0].to_output("numba")


def _convert_to_gpuarray_c(data):
    return _convert_to_gpuarray(data, order='C')


def _sparsify_and_convert(data, input_type, sparsity_ratio=0.3):
    """Randomly set values to 0 and produce a sparse array."""
    if not has_scipy():
        raise RuntimeError("Scipy is required")
    import scipy
    random_loc = np.random.choice(data.size,
                                  int(data.size * sparsity_ratio),
                                  replace=False)
    data.ravel()[random_loc] = 0
    if input_type == 'csr':
        return scipy.sparse.csr_matrix(data)
    elif input_type == 'csc':
        return scipy.sparse.csc_matrix(data)
    else:
        TypeError('Wrong sparse input type {}'.format(input_type))


def _convert_to_scipy_sparse(data, input_type):
    """Returns a tuple of arrays. Each of the arrays
    have some of its values being set randomly to 0,
    it is then converted to a scipy sparse array"""
    if data is None:
        return None
    elif isinstance(data, tuple):
        return tuple([_convert_to_scipy_sparse(d, input_type) for d in data])
    elif isinstance(data, np.ndarray):
        return _sparsify_and_convert(data, input_type)
    elif isinstance(data, cudf.DataFrame):
        return _sparsify_and_convert(data.as_matrix(), input_type)
    elif isinstance(data, cudf.Series):
        return _sparsify_and_convert(data.to_array(), input_type)
    elif isinstance(data, (pd.DataFrame, pd.Series)):
        return _sparsify_and_convert(data.to_numpy(), input_type)
    else:
        raise Exception("Unsupported type %s" % str(type(data)))


def _convert_to_scipy_sparse_csr(data):
    return _convert_to_scipy_sparse(data, 'csr')


def _convert_to_scipy_sparse_csc(data):
    return _convert_to_scipy_sparse(data, 'csc')


_data_generators = {
    'blobs': _gen_data_blobs,
    'zeros': _gen_data_zeros,
    'classification': _gen_data_classification,
    'regression': _gen_data_regression,
    'bosch': _gen_data_bosch,
    'covtype': _gen_data_covtype,
    'epsilon': _gen_data_epsilon,
    'higgs': _gen_data_higgs,
    'year': _gen_data_year,
}
_data_converters = {
    'numpy': _convert_to_numpy,
    'cudf': _convert_to_cudf,
    'pandas': _convert_to_pandas,
    'gpuarray': _convert_to_gpuarray,
    'gpuarray-c': _convert_to_gpuarray_c,
    'scipy-sparse-csr': _convert_to_scipy_sparse_csr,
    'scipy-sparse-csc': _convert_to_scipy_sparse_csc
}


def all_datasets():
    return _data_generators


@functools.lru_cache(maxsize=8)
def gen_data(
    dataset_name,
    dataset_format,
    n_samples=0,
    n_features=0,
    random_state=42,
    test_fraction=0.0,
    **kwargs
):
    """Returns a tuple of data from the specified generator.

    Output
    -------
        (train_features, train_labels, test_features, test_labels) tuple
        containing matrices or dataframes of the requested format.
        test_features and test_labels may be None if no splitting was done.

    Parameters
    ----------
    dataset_name : str
        Dataset to use. Can be a synthetic generator (blobs or regression)
        or a specified dataset (higgs currently, others coming soon)

    dataset_format : str
        Type of data to return. (One of cudf, numpy, pandas, gpuarray)

    n_samples : int
        Number of samples to include in training set (regardless of test split)
    test_fraction : float
        Fraction of the dataset to partition randomly into the test set.
        If this is 0.0, no test set will be created.
    """
    data = _data_generators[dataset_name](
        int(n_samples / (1 - test_fraction)),
        n_features,
        random_state,
        **kwargs
    )
    if test_fraction != 0.0:
        if n_samples == 0:
            n_samples = int(data[0].shape[0] * (1 - test_fraction))
        X_train, X_test, y_train, y_test = tuple(
            sklearn.model_selection.train_test_split(
                *data, train_size=n_samples, random_state=random_state
            )
        )
        data = (X_train, y_train, X_test, y_test)
    else:
        data = (*data, None, None)  # No test set

    data = _data_converters[dataset_format](data)
    return data
