/*
 * Copyright (c) 2019-2021, NVIDIA CORPORATION.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/** @file common.cuh Common GPU functionality */
#pragma once

#include <stdio.h>
#include <stdlib.h>
#include <cub/cub.cuh>
#include <stdexcept>
#include <string>

#include <cuml/fil/fil.h>
#include <raft/cuda_utils.cuh>

#include "internal.cuh"

namespace ML {
namespace fil {

__host__ __device__ __forceinline__ int tree_num_nodes(int depth) {
  return (1 << (depth + 1)) - 1;
}

__host__ __device__ __forceinline__ int forest_num_nodes(int num_trees,
                                                         int depth) {
  return num_trees * tree_num_nodes(depth);
}

template <>
__host__ __device__ __forceinline__ float base_node::output<float>() const {
  return val.f;
}
template <>
__host__ __device__ __forceinline__ int base_node::output<int>() const {
  return val.idx;
}

/** dense_tree represents a dense tree */
struct dense_tree {
  __host__ __device__ dense_tree(dense_node* nodes, int node_pitch)
    : nodes_(nodes), node_pitch_(node_pitch) {}
  __host__ __device__ const dense_node& operator[](int i) const {
    return nodes_[i * node_pitch_];
  }
  dense_node* nodes_ = nullptr;
  int node_pitch_ = 0;
};

/** dense_storage stores the forest as a collection of dense nodes */
struct dense_storage {
  __host__ __device__ dense_storage(dense_node* nodes, int num_trees,
                                    int tree_stride, int node_pitch)
    : nodes_(nodes),
      num_trees_(num_trees),
      tree_stride_(tree_stride),
      node_pitch_(node_pitch) {}
  __host__ __device__ int num_trees() const { return num_trees_; }
  __host__ __device__ dense_tree operator[](int i) const {
    return dense_tree(nodes_ + i * tree_stride_, node_pitch_);
  }
  dense_node* nodes_ = nullptr;
  int num_trees_ = 0;
  int tree_stride_ = 0;
  int node_pitch_ = 0;
};

/** sparse_tree is a sparse tree */
template <typename node_t>
struct sparse_tree {
  __host__ __device__ sparse_tree(node_t* nodes) : nodes_(nodes) {}
  __host__ __device__ const node_t& operator[](int i) const {
    return nodes_[i];
  }
  node_t* nodes_ = nullptr;
};

/** sparse_storage stores the forest as a collection of sparse nodes */
template <typename node_t>
struct sparse_storage {
  int* trees_ = nullptr;
  node_t* nodes_ = nullptr;
  int num_trees_ = 0;
  __host__ __device__ sparse_storage(int* trees, node_t* nodes, int num_trees)
    : trees_(trees), nodes_(nodes), num_trees_(num_trees) {}
  __host__ __device__ int num_trees() const { return num_trees_; }
  __host__ __device__ sparse_tree<node_t> operator[](int i) const {
    return sparse_tree<node_t>(&nodes_[trees_[i]]);
  }
};

typedef sparse_storage<sparse_node16> sparse_storage16;
typedef sparse_storage<sparse_node8> sparse_storage8;

/// all model parameters mostly required to compute shared memory footprint,
/// also the footprint itself
struct shmem_size_params {
  /// for class probabilities, this is the number of classes considered;
  /// num_classes is ignored otherwise
  int num_classes = 1;
  // leaf_algo determines what the leaves store (predict) and how FIL
  // aggregates them into class margins/predicted class/regression answer
  leaf_algo_t leaf_algo = leaf_algo_t::FLOAT_UNARY_BINARY;
  /// how many columns an input row has
  int num_cols = 0;
  /// whether to predict class probabilities or classes (or regress)
  bool predict_proba = false;
  /// are the input columns are prefetched into shared
  /// memory before inferring the row in question
  bool cols_in_shmem = true;
  /// n_items is the most items per thread that fit into shared memory
  int n_items = 0;
  /// max_shm is the maximum opt-in shared memory on the device
  int max_shm = 0;
  // blockdim_x is the CUDA block size
  int blockdim_x = 0;
  /// shm_sz is the associated shared memory footprint
  int shm_sz = INT_MAX;

  __host__ __device__ size_t cols_shmem_size() {
    return cols_in_shmem ? sizeof(float) * num_cols * n_items : 0;
  }
  template <int NITEMS, leaf_algo_t leaf_algo>
  size_t get_smem_footprint();
};

// predict_params are parameters for prediction
struct predict_params : shmem_size_params {
  predict_params(shmem_size_params ssp) : shmem_size_params(ssp) {}
  // Model parameters.
  algo_t algo;
  // number of outputs for the forest per each data row
  int num_outputs;

  // Data parameters.
  float* preds;
  const float* data;
  // number of data rows (instances) to predict on
  size_t num_rows;

  // to signal infer kernel to apply softmax and also average prior to that
  // for GROVE_PER_CLASS for predict_proba
  output_t transform;
  int num_blocks;
};

namespace dispatch {

template <template <bool, leaf_algo_t, int> class Func, typename storage_type,
          bool cols_in_shmem, leaf_algo_t leaf_algo, int n_items,
          typename... Args>
void dispatch_final(predict_params params, Args... args) {
  Func<cols_in_shmem, leaf_algo, n_items>::template run<storage_type>(params,
                                                                      args...);
}

template <template <bool, leaf_algo_t, int> class Func, typename storage_type,
          bool cols_in_shmem, leaf_algo_t leaf_algo, typename... Args>
void dispatch_on_n_items(predict_params params, Args... args) {
  switch (params.n_items) {
    case 1:
      dispatch_final<Func, storage_type, cols_in_shmem, leaf_algo, 1>(params,
                                                                      args...);
      break;
    case 2:
      dispatch_final<Func, storage_type, cols_in_shmem, leaf_algo, 2>(params,
                                                                      args...);
      break;
    case 3:
      dispatch_final<Func, storage_type, cols_in_shmem, leaf_algo, 3>(params,
                                                                      args...);
      break;
    case 4:
      dispatch_final<Func, storage_type, cols_in_shmem, leaf_algo, 4>(params,
                                                                      args...);
      break;
    default:
      ASSERT(false, "internal error: n_items > 4");
  }
}

template <template <bool, leaf_algo_t, int> class Func, typename storage_type,
          bool cols_in_shmem, typename... Args>
void dispatch_on_leaf_algo(predict_params params, Args... args) {
  switch (params.leaf_algo) {
    case FLOAT_UNARY_BINARY:
      params.blockdim_x = FIL_TPB;
      dispatch_on_n_items<Func, storage_type, cols_in_shmem,
                          FLOAT_UNARY_BINARY>(params, args...);
      break;
    case GROVE_PER_CLASS:
      if (params.num_classes > FIL_TPB) {
        params.leaf_algo = GROVE_PER_CLASS_MANY_CLASSES;
        params.blockdim_x = FIL_TPB;
        dispatch_on_n_items<Func, storage_type, cols_in_shmem,
                            GROVE_PER_CLASS_MANY_CLASSES>(params, args...);
      } else {
        params.leaf_algo = GROVE_PER_CLASS_FEW_CLASSES;
        params.blockdim_x = FIL_TPB - FIL_TPB % params.num_classes;
        dispatch_on_n_items<Func, storage_type, cols_in_shmem,
                            GROVE_PER_CLASS_FEW_CLASSES>(params, args...);
      }
      break;
    case CATEGORICAL_LEAF:
      params.blockdim_x = FIL_TPB;
      dispatch_on_n_items<Func, storage_type, cols_in_shmem, CATEGORICAL_LEAF>(
        params, args...);
      break;
    default:
      ASSERT(false, "internal error: invalid leaf_algo");
  }
}

template <template <bool, leaf_algo_t, int> class Func, typename storage_type,
          typename... Args>
void dispatch_on_cols_in_shmem(predict_params params, Args... args) {
  if (params.cols_in_shmem)
    dispatch_on_leaf_algo<Func, storage_type, true>(params, args...);
  else
    dispatch_on_leaf_algo<Func, storage_type, false>(params, args...);
}

}  // namespace dispatch

template <template <bool, leaf_algo_t, int> class Func, typename storage_type,
          typename... Args>
void dispatch_on_FIL_template_params(predict_params params, Args... args) {
  dispatch::dispatch_on_cols_in_shmem<Func, storage_type>(params, args...);
}

// infer() calls the inference kernel with the parameters on the stream
template <typename storage_type>
void infer(storage_type forest, predict_params params, cudaStream_t stream);

}  // namespace fil
}  // namespace ML
