#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
from __future__ import annotations

import os
import types
import warnings
from typing import List, Optional, Tuple, Union

import torch
from torch import nn

from . import privacy_analysis as tf_privacy
from .dp_model_inspector import DPModelInspector
from .per_sample_gradient_clip import PerSampleGradientClipper
from .utils import clipping


class PrivacyEngine:
    r"""
    The main component of Pytorch DP is the ``PrivacyEngine``.

    To train a model with differential privacy, all you need to do
    is to define a ``PrivacyEngine`` and later attach it to your
    optimizer before running.


    Example
    -------
    This example shows how to define a ``PrivacyEngine`` and to attach
    it to your optimizer.

    >>> import torch
    >>> model = torch.nn.Linear(16, 32)  # An example model
    >>> optimizer = torch.optim.SGD(model.parameters(), lr=0.05)
    >>> privacy_engine = PrivacyEngine(model, batch_size, sample_size, alphas=range(2,32), noise_multiplier=1.3, max_grad_norm=1.0)
    >>> privacy_engine.attach(optimizer)  # That's it! Now it's business as usual.
    """

    def __init__(
        self,
        module: nn.Module,
        batch_size: int,
        sample_size: int,
        alphas: List[float],
        noise_multiplier: float,
        max_grad_norm: Union[float, List[float]],
        grad_norm_type: int = 2,
        batch_first: bool = True,
        target_delta: float = 1e-6,
        loss_reduction: str = "mean",
        **misc_settings,
    ):
        r"""
        Parameters
        ----------
        module : nn.Module
            The Pytorch module to which we are attaching the privacy engine
        batch_size : int
            Batch size
        sample_size : int
            The size of the sample (dataset). Used in the privacy accountant.
        alphas : List[float]
            A list of RDP orders
        noise_multiplier : float
            The ratio of the standard deviation of the Gaussian noise to
            the L2-sensitivity of the function to which the noise is added
        max_grad_norm : Union[float, List[float]]
            The maximum norm of the per-sample gradients. Any gradient with norm
            higher than this will be clipped to this value.
        grad_norm_type : int
            The order of the norm. For instance, 2 represents L-2 norm, while
            1 represents L-1 norm.
        batch_first : bool
            Flag to indicate if the input tensor to the corresponding module
            has the first dimension representing the batch. If set to True,
            dimensions on input tensor will be ``[batch_size, ..., ...]``.
        target_delta : float
            The target delta
        loss_reduction : str
            Indicates if the loss reduction (for aggregating the gradients)
            is a sum or a mean operation. Can take values "sum" or "mean"
        **misc_settings
            Other arguments to the init
        """
        self.steps = 0
        self.module = module
        self.alphas = alphas
        self.device = next(module.parameters()).device
        self.batch_size = batch_size
        self.sample_rate = batch_size / sample_size
        self.noise_multiplier = noise_multiplier
        self.max_grad_norm = max_grad_norm
        self.grad_norm_type = grad_norm_type
        self.batch_first = batch_first
        self.target_delta = target_delta

        self._set_seed(None)
        self.validator = DPModelInspector()
        self.clipper = None  # lazy initialization in attach
        self.misc_settings = misc_settings

        self.loss_reduction = loss_reduction

    def detach(self):
        r"""
        Detaches the privacy engine from optimizer.

        To detach the ``PrivacyEngine`` from optimizer, this method returns
        the model and the optimizer to their original states (i.e. all
        added attributes/methods will be removed).
        """
        optim = self.optimizer
        optim.privacy_engine = None
        self.clipper.close()
        optim.step = types.MethodType(optim.original_step, optim)
        del optim.virtual_step

    def attach(self, optimizer: torch.optim.Optimizer):
        r"""
        Attaches the privacy engine to the optimizer.

        Attaches to the ``PrivacyEngine`` an optimizer object,and injects
        itself into the optimizer's step. To do that it,

        1. Validates that the model does not have unsupported layers.

        2. Adds a pointer to this object (the ``PrivacyEngine``) inside the optimizer.

        3. Moves optimizer's original ``step()`` function to ``original_step()``.

        4. Monkeypatches the optimizer's ``step()`` function to call ``step()`` on
        the query engine automatically whenever it would call ``step()`` for itself.

        Parameters
        ----------
        optimizer : torch.optim.Optimizer
            The optimizer to which the privacy engine will attach
        """

        self.validator.validate(self.module)
        norm_clipper = (
            # pyre-fixme[6]: Expected `float` for 1st param but got
            #  `Union[List[float], float]`.
            clipping.ConstantFlatClipper(self.max_grad_norm)
            if not isinstance(self.max_grad_norm, list)
            # pyre-fixme[6]: Expected `List[float]` for 1st param but got
            #  `Union[List[float], float]`.
            else clipping.ConstantPerLayerClipper(self.max_grad_norm)
        )

        if self.misc_settings.get("experimental", False):
            norm_clipper = clipping._Dynamic_Clipper_(
                # pyre-fixme[6]: Expected `List[float]` for 1st param but got
                #  `List[Union[List[float], float]]`.
                [self.max_grad_norm],
                self.misc_settings.get("clip_per_layer", False),
                self.misc_settings.get(
                    "clipping_method", clipping.ClippingMethod.STATIC
                ),
                self.misc_settings.get("ratio", 0.0),
            )

        self.clipper = PerSampleGradientClipper(
            self.module, norm_clipper, self.batch_first
        )

        def dp_step(self, closure=None):
            self.privacy_engine.step()
            self.original_step(closure)

        # pyre-fixme[16]: `Optimizer` has no attribute `privacy_engine`.
        optimizer.privacy_engine = self
        # pyre-fixme[16]: `Optimizer` has no attribute `original_step`.
        optimizer.original_step = optimizer.step
        # pyre-fixme[8]: Attribute has type
        #  `BoundMethod[typing.Callable(torch.optim.Optimizer.step)[[Named(self,
        #  torch.optim.Optimizer), Named(closure, typing.Optional[typing.Callable[[],
        #  torch.Tensor]], default)], typing.Optional[torch.Tensor]],
        #  torch.optim.Optimizer]`; used as `MethodType`.
        optimizer.step = types.MethodType(dp_step, optimizer)

        def virtual_step(self):
            self.privacy_engine.virtual_step()

        # pyre-fixme[16]: `Optimizer` has no attribute `virtual_step`.
        optimizer.virtual_step = types.MethodType(virtual_step, optimizer)

        # pyre-fixme[16]: `PrivacyEngine` has no attribute `optimizer`.
        self.optimizer = optimizer  # create a cross reference for detaching

    def get_renyi_divergence(self):
        rdp = torch.tensor(
            tf_privacy.compute_rdp(
                self.sample_rate, self.noise_multiplier, 1, self.alphas
            )
        )
        return rdp

    def get_privacy_spent(
        self, target_delta: Optional[float] = None
    ) -> Tuple[float, float]:
        """
        Computes the (epsilon, delta) privacy budget spent so far.

        This method converts from an (alpha, epsilon)-DP guarantee for all alphas that
        the ``PrivacyEngine`` was initialized with. It returns the optimal alpha together
        with the best epsilon.

        Parameters
        ----------
        target_delta : Optional[float]
            The Target delta. If None, it will default to the privacy engine's target delta.

        Returns
        -------
        Tuple[float, float]
            Pair of epsilon and optimal order alpha.
        """
        if target_delta is None:
            target_delta = self.target_delta
        rdp = self.get_renyi_divergence() * self.steps
        return tf_privacy.get_privacy_spent(self.alphas, rdp, target_delta)

    def step(self):
        """
        Takes a step for the privacy engine.

        Notes
        -----
        You should not call this method directly. Rather, by attaching your
        ``PrivacyEngine`` to the optimizer, the ``PrivacyEngine`` would have the
        optimizer call this method for you.

        Raises
        ------
        ValueError
            If the last batch of training epoch is greater than others.
            This ensures the clipper consumed the right amount of gradients.
            In the last batch of a training epoch, we might get a batch that is
            smaller than others but we should never get a batch that is too large

        """
        self.steps += 1
        self.clipper.clip_and_accumulate()
        clip_values, batch_size = self.clipper.pre_step()

        if batch_size > self.batch_size:
            raise ValueError(
                f"PrivacyEngine expected a batch of size {self.batch_size} "
                f"but received a batch of size {batch_size}"
            )

        if batch_size < self.batch_size:
            warnings.warn(
                f"PrivacyEngine expected a batch of size {self.batch_size} "
                f"but the last step received a batch of size {batch_size}. "
                "This means that the privacy analysis will be a bit more "
                "pessimistic. You can set `drop_last = True` in your PyTorch "
                "dataloader to avoid this problem completely"
            )

        params = (p for p in self.module.parameters() if p.requires_grad)
        for p, clip_value in zip(params, clip_values):
            noise = self._generate_noise(clip_value, p)
            if self.loss_reduction == "mean":
                noise /= batch_size
            p.grad += noise

    def to(self, device: Union[str, torch.device]) -> PrivacyEngine:
        """
        Moves the privacy engine to the target device.

        Parameters
        ----------
        device : Union[str, torch.device]
            The device on which Pytorch Tensors are allocated.
            See: https://pytorch.org/docs/stable/tensor_attributes.html#torch.torch.device

        Example
        -------
        This example shows the usage of this method, on how to move the model
        after instantiating the ``PrivacyEngine``.

        >>> model = torch.nn.Linear(16, 32)  # An example model. Default device is CPU
        >>> privacy_engine = PrivacyEngine(model, batch_size, sample_size, alphas=range(5,64), noise_multiplier=0.8, max_grad_norm=0.5)
        >>> device = "cuda:3"  # GPU
        >>> model.to(device)  # If we move the model to GPU, we should call the to() method of the privacy engine (next line)
        >>> privacy_engine.to(device)

        Returns
        -------
        PrivacyEngine
            This privacy engine
        """
        self.device = device
        return self

    def virtual_step(self):
        r"""
        Takes a virtual step.

        Virtual batches enable training with arbitrary large batch sizes, while
        keeping the memory consumption constant. This is beneficial, when training
        models with larger batch sizes than standard models.

        Example
        -------
        Imagine you want to train a model with batch size of 2048, but you can only
        fit batch size of 128 in your GPU. Then, you can do the following:

        >>> for i, (X, y) in enumerate(dataloader):
        >>>     logits = model(X)
        >>>     loss = criterion(logits, y)
        >>>     loss.backward()
        >>>     if i % 16 == 15:
        >>>         optimizer.step()    # this will call privacy engine's step()
        >>>         optimizer.zero_grad()
        >>>     else:
        >>>         optimizer.virtual_step()   # this will call privacy engine's virtual_step()

        The rough idea of virtual step is as follows:

        1. Calling ``loss.backward()`` repeatedly stores the per-sample gradients
        for all mini-batches. If we call ``loss.backward()`` ``N`` times on
        mini-batches of size ``B``, then each weight's ``.grad_sample`` field will
        contain ``NxB`` gradients. Then, when calling ``step()``, the privacy engine
        clips all ``NxB`` gradients and computes the average gradient for an effective
        batch of size ``NxB``. A call to ``optimizer.zero_grad()`` erases the
        per-sample gradients.

        2. By calling ``virtual_step()`` after ``loss.backward()``,the ``B``
        per-sample gradients for this mini-batch are clipped and summed up into a
        gradient accumulator. The per-sample gradients can then be discarded. After
        ``N`` iterations (alternating calls to ``loss.backward()`` and
        ``virtual_step()``), a call to ``step()`` will compute the average gradient
        for an effective batch of size ``NxB``.

        The advantage here is that this is memory-efficient: it discards the per-sample
        gradients after every mini-batch. We can thus handle batches of arbitrary size.
        """
        self.clipper.clip_and_accumulate()

    def _generate_noise(
        self, max_grad_norm: float, reference: nn.parameter.Parameter
    ) -> torch.Tensor:
        r"""
        Generates a tensor of Gaussian noise of the same shape as ``reference``.

        The generated tensor has zero mean and standard deviation
        sigma = ``noise_multiplier x max_grad_norm ``

        Parameters
        ----------
        max_grad_norm : float
            The maximum norm of the per-sample gradients.
        reference : torch.nn.parameter.Parameter
            The reference, based on which the dimention of the noise tensor
            will be determined

        Returns
        -------
        torch.Tensor
            the generated noise with noise zero and standard
            deviation of ``noise_multiplier x max_grad_norm ``
        """
        if self.noise_multiplier > 0:
            return torch.normal(
                0,
                self.noise_multiplier * max_grad_norm,
                # pyre-fixme[16]: nn.parameter.Parameter has no attribute grad
                reference.grad.shape,
                device=self.device,
                generator=self.secure_generator,
            )
        # pyre-fixme[7]: Expected `Tensor` but got `float`.
        return 0.0

    def _set_seed(self, secure_seed: Optional[int]):
        r"""
        Allows to manually set the seed allowing for a deterministic run.

        WARNING: MANUALLY SETTING THE SEED BREAKS THE GUARANTEE OF A SECURE SEED.
        If you elect to do that, your application will own guaranteeing the safety
        of your pseudo-random number generator.

        Parameters
        ----------
        secure_seed : int
            The secure seed
        """
        if secure_seed is not None:
            warnings.warn(
                "Seed was manually set. This prevents us from generating "
                "a cryptographically secure pseudorandom number generator "
                "seed. Hence, we cannot guarantee the safety of random "
                "number generation process."
            )
            # pyre-fixme[16]: `PrivacyEngine` has no attribute `secure_seed`.
            self.secure_seed = secure_seed
        else:
            self.secure_seed = int.from_bytes(
                os.urandom(8), byteorder="big", signed=True
            )
        self.secure_generator = (
            torch.random.manual_seed(self.secure_seed)
            if self.device.type == "cpu"
            else torch.cuda.manual_seed(self.secure_seed)
        )
