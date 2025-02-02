# Copyright 2022 Google LLC.
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

"""Set of utilities for LightweighMMM package."""
import pickle
import time
from typing import Any, Tuple

from absl import logging
from jax import random
import jax.numpy as jnp
import numpy as np
from scipy import interpolate
from scipy import optimize
from scipy import spatial
from scipy import stats
from tensorflow.io import gfile

from lightweight_mmm import media_transforms


def save_model(
    media_mix_model: Any,
    file_path: str
    ) -> None:
  """Saves the given model in the given path.

  Args:
    media_mix_model: Model to save on disk.
    file_path: File path where the model should be placed.
  """
  with gfile.GFile(file_path, "wb") as file:
    pickle.dump(obj=media_mix_model, file=file)


def load_model(file_path: str) -> Any:
  """Loads a model given a string path.

  Args:
    file_path: Path of the file containing the model.

  Returns:
    The LightweightMMM object that was stored in the given path.
  """
  with gfile.GFile(file_path, "rb") as file:
    media_mix_model = pickle.load(file=file)

  for attr in dir(media_mix_model):
    if attr.startswith("__"):
      continue
    attr_value = getattr(media_mix_model, attr)
    if isinstance(attr_value, np.ndarray):
      setattr(media_mix_model, attr, jnp.array(attr_value))

  return media_mix_model


def get_time_seed() -> int:
  """Generates an integer using the last decimals of time.time().

  Returns:
    Integer to be used as seed.
  """
  # time.time() has the following format: 1645174953.0429401
  return int(str(time.time()).split(".")[1])


def simulate_dummy_data(
    data_size: int,
    n_media_channels: int,
    n_extra_features: int,
    geos: int = 1,
    seed: int = 0
    ) -> Tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
  """Simulates dummy data needed for media mix modelling.

  This function's goal is to be super simple and not have many parameters,
  although it does not generate a fully realistic dataset is only meant to be
  used for demos/tutorial purposes. Uses carryover for lagging but has no
  saturation and no trend.

  The data simulated includes the media data, extra features, a target/KPI and
  costs.

  Args:
    data_size: Number of rows to generate.
    n_media_channels: Number of media channels to generate.
    n_extra_features: Number of extra features to generate.
    geos: Number of geos for geo level data (default = 1 for national).
    seed: Random seed.

  Returns:
    The simulated media, extra features, target and costs.
  """
  if data_size < 1 or n_media_channels < 1 or n_extra_features < 1:
    raise ValueError(
        "Data size, n_media_channels and n_extra_features must be greater than"
        " 0. Please check the values introduced are greater than zero.")
  data_offset = int(data_size * 0.2)
  data_size += data_offset
  key = random.PRNGKey(seed)
  sub_keys = random.split(key=key, num=7)
  media_data = random.normal(key=sub_keys[0],
                             shape=(data_size, n_media_channels)) * 2 + 20

  extra_features = random.normal(key=sub_keys[1],
                                 shape=(data_size, n_extra_features)) + 5
  # Reduce the costs to make ROI realistic.
  costs = media_data[data_offset:].sum(axis=0) * .1

  seasonality = media_transforms.calculate_seasonality(
      number_periods=data_size,
      degrees=2,
      frequency=52,
      gamma_seasonality=1)
  target_noise = random.normal(key=sub_keys[2], shape=(data_size,)) + 3

  # media_data_transformed = media_transforms.adstock(media_data)
  media_data_transformed = media_transforms.carryover(media_data)
  beta_media = random.normal(key=sub_keys[3], shape=(n_media_channels,)) + 1
  beta_extra_features = random.normal(key=sub_keys[4],
                                      shape=(n_extra_features,))
  # There is no trend to keep this very simple.
  target = 10 + seasonality + media_data_transformed.dot(
      beta_media) + extra_features.dot(beta_extra_features) + target_noise

  logging.info("Correlation between transformed media and target")
  logging.info([
      np.corrcoef(target[data_offset:], media_data_transformed[data_offset:,
                                                               i])[0, 1]
      for i in range(n_media_channels)
  ])

  logging.info("True ROI for media channels")
  logging.info([
      sum(media_data_transformed[data_offset:, i] * beta_media[i]) / costs[i]
      for i in range(n_media_channels)
  ])

  if geos > 1:
    # Distribute national data to geo and add some more noise.
    weights = random.uniform(key=sub_keys[5], shape=(1, geos))
    weights /= sum(weights)
    target_noise = random.normal(key=sub_keys[6], shape=(data_size, geos)) * .5
    target = target[:, np.newaxis].dot(weights) + target_noise
    media_data = media_data[:, :, np.newaxis].dot(weights)
    extra_features = extra_features[:, :, np.newaxis].dot(weights)

  return (
      media_data[data_offset:],
      extra_features[data_offset:],
      target[data_offset:],
      costs)


def get_halfnormal_mean_from_scale(scale: float) -> float:
  """Returns the mean of the half-normal distribition."""
  # https://en.wikipedia.org/wiki/Half-normal_distribution
  return scale * np.sqrt(2) / np.sqrt(np.pi)


def get_halfnormal_scale_from_mean(mean: float) -> float:
  """Returns the scale of the half-normal distribution."""
  # https://en.wikipedia.org/wiki/Half-normal_distribution
  return mean * np.sqrt(np.pi) / np.sqrt(2)


def get_beta_params_from_mu_sigma(mu: float,
                                  sigma: float,
                                  bracket: Tuple[float, float] = (.5, 100.)
                                  ) -> Tuple[float, float]:
  """Deterministically estimates (a, b) from (mu, sigma) of a beta variable.

  https://en.wikipedia.org/wiki/Beta_distribution

  Args:
    mu: The sample mean of the beta distributed variable.
    sigma: The sample standard deviation of the beta distributed variable.
    bracket: Search bracket for b.

  Returns:
    Tuple of the (a, b) parameters.
  """
  # Assume a = 1 to find b.
  def _f(x):
    return x ** 2 + 4 * x + 5 + 2 / x - 1 / sigma ** 2
  b = optimize.root_scalar(_f, bracket=bracket, method="brentq").root
  # Given b, now find a better a.
  a = b / (1 / mu - 1)
  return a, b


def _estimate_pdf(p: jnp.ndarray, x: jnp.ndarray) -> jnp.ndarray:
  """Estimates smooth pdf with Gaussian kernel.

  Args:
    p: Samples.
    x: The continuous x space (sorted).

  Returns:
    A density vector.
  """
  density = sum(stats.norm(xi).pdf(x) for xi in p)
  return density / density.sum()


def _pmf(p: jnp.ndarray, x: jnp.ndarray) -> jnp.ndarray:
  """Estimates discrete pmf.

  Args:
    p: Samples.
    x: The discrete x space (sorted).

  Returns:
    A pmf vector.
  """
  p_cdf = jnp.array([jnp.sum(p <= x[i]) for i in range(len(x))])
  p_pmf = np.concatenate([[p_cdf[0]], jnp.diff(p_cdf)])
  return p_pmf / p_pmf.sum()


def distance_pior_posterior(p: jnp.ndarray, q: jnp.ndarray, method: str = "KS",
                            discrete: bool = True) -> float:
  """Quantifies the distance between two distributions.

  Note we do not use KL divergence because it's not defined when a probability
  is 0.

  https://en.wikipedia.org/wiki/Hellinger_distance

  Args:
    p: Samples for distribution 1.
    q: Samples for distribution 2.
    method: We can have four methods: KS, Hellinger, JS and min.
    discrete: Whether input data is discrete or continuous.

  Returns:
    The distance metric (between 0 and 1).
  """

  if method == "KS":
    # https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.ks_2samp.html
    return stats.ks_2samp(p, q).statistic
  elif method in ["Hellinger", "JS", "min"]:
    if discrete:
      x = jnp.unique(jnp.concatenate((p, q)))
      p_pdf = _pmf(p, x)
      q_pdf = _pmf(q, x)
    else:
      minx, maxx = min(p.min(), q.min()), max(p.max(), q.max())
      x = np.linspace(minx, maxx, 100)
      p_pdf = _estimate_pdf(p, x)
      q_pdf = _estimate_pdf(q, x)
  if method == "Hellinger":
    return np.sqrt(jnp.sum((np.sqrt(p_pdf) - np.sqrt(q_pdf)) ** 2)) / np.sqrt(2)
  elif method == "JS":
    # https://docs.scipy.org/doc/scipy/reference/generated/scipy.spatial.distance.jensenshannon.html
    return spatial.distance.jensenshannon(p_pdf, q_pdf)
  else:
    return 1 - np.minimum(p_pdf, q_pdf).sum()


def interpolate_outliers(x: jnp.ndarray,
                         outlier_idx: jnp.ndarray) -> jnp.ndarray:
  """Overwrites outliers in x with interpolated values.

  Args:
    x: The original univariate variable with outliers.
    outlier_idx: Indices of the outliers in x.

  Returns:
    A cleaned x with outliers overwritten.

  """
  time_idx = jnp.arange(len(x))
  inverse_idx = jnp.array([i for i in range(len(x)) if i not in outlier_idx])
  interp_func = interpolate.interp1d(
      time_idx[inverse_idx], x[inverse_idx], kind="linear")
  x = x.at[outlier_idx].set(interp_func(time_idx[outlier_idx]))
  return x
