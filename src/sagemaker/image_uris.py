# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
"""Functions for generating ECR image URIs for pre-built SageMaker Docker images."""
from __future__ import absolute_import

import json
import logging
import os
import re

from sagemaker import utils
from sagemaker.spark import defaults

logger = logging.getLogger(__name__)

ECR_URI_TEMPLATE = "{registry}.dkr.{hostname}/{repository}"
HUGGING_FACE_FRAMEWORK = "huggingface"


def retrieve(
    framework,
    region,
    version=None,
    py_version=None,
    instance_type=None,
    accelerator_type=None,
    image_scope=None,
    container_version=None,
    distribution=None,
    base_framework_version=None,
    training_compiler_config=None,
):
    """Retrieves the ECR URI for the Docker image matching the given arguments.

    Ideally this function should not be called directly, rather it should be called from the
    fit() function inside framework estimator.

    Args:
        framework (str): The name of the framework or algorithm.
        region (str): The AWS region.
        version (str): The framework or algorithm version. This is required if there is
            more than one supported version for the given framework or algorithm.
        py_version (str): The Python version. This is required if there is
            more than one supported Python version for the given framework version.
        instance_type (str): The SageMaker instance type. For supported types, see
            https://aws.amazon.com/sagemaker/pricing/instance-types. This is required if
            there are different images for different processor types.
        accelerator_type (str): Elastic Inference accelerator type. For more, see
            https://docs.aws.amazon.com/sagemaker/latest/dg/ei.html.
        image_scope (str): The image type, i.e. what it is used for.
            Valid values: "training", "inference", "eia". If ``accelerator_type`` is set,
            ``image_scope`` is ignored.
        container_version (str): the version of docker image.
            Ideally the value of parameter should be created inside the framework.
            For custom use, see the list of supported container versions:
            https://github.com/aws/deep-learning-containers/blob/master/available_images.md
            (default: None).
        distribution (dict): A dictionary with information on how to run distributed training
        training_compiler_config (:class:`~sagemaker.training_compiler.TrainingCompilerConfig`):
            A configuration class for the SageMaker Training Compiler
            (default: None).

    Returns:
        str: the ECR URI for the corresponding SageMaker Docker image.

    Raises:
        ValueError: If the combination of arguments specified is not supported.
    """
    if training_compiler_config is None:
        config = _config_for_framework_and_scope(framework, image_scope, accelerator_type)
    elif framework == HUGGING_FACE_FRAMEWORK:
        config = _config_for_framework_and_scope(
            framework + "-training-compiler", image_scope, accelerator_type
        )
    else:
        raise ValueError(
            "Unsupported Configuration: Training Compiler is only supported with HuggingFace"
        )
    original_version = version
    version = _validate_version_and_set_if_needed(version, config, framework)
    version_config = config["versions"][_version_for_config(version, config)]

    if framework == HUGGING_FACE_FRAMEWORK:
        if version_config.get("version_aliases"):
            full_base_framework_version = version_config["version_aliases"].get(
                base_framework_version, base_framework_version
            )

        _validate_arg(full_base_framework_version, list(version_config.keys()), "base framework")
        version_config = version_config.get(full_base_framework_version)

    py_version = _validate_py_version_and_set_if_needed(py_version, version_config, framework)
    version_config = version_config.get(py_version) or version_config
    registry = _registry_from_region(region, version_config["registries"])
    hostname = utils._botocore_resolver().construct_endpoint("ecr", region)["hostname"]

    repo = version_config["repository"]

    processor = _processor(
        instance_type, config.get("processors") or version_config.get("processors")
    )

    # if container version is available in .json file, utilize that
    if version_config.get("container_version"):
        container_version = version_config["container_version"][processor]

    if framework == HUGGING_FACE_FRAMEWORK:
        pt_or_tf_version = (
            re.compile("^(pytorch|tensorflow)(.*)$").match(base_framework_version).group(2)
        )

        _version = original_version
        if repo in [
            "huggingface-pytorch-trcomp-training",
            "huggingface-tensorflow-trcomp-training",
        ]:
            _version = version

        tag_prefix = f"{pt_or_tf_version}-transformers{_version}"

    else:
        tag_prefix = version_config.get("tag_prefix", version)

    tag = _format_tag(
        tag_prefix,
        processor,
        py_version,
        container_version,
    )

    if _should_auto_select_container_version(instance_type, distribution):
        container_versions = {
            "tensorflow-2.3-gpu-py37": "cu110-ubuntu18.04-v3",
            "tensorflow-2.3.1-gpu-py37": "cu110-ubuntu18.04",
            "tensorflow-2.3.2-gpu-py37": "cu110-ubuntu18.04",
            "tensorflow-1.15-gpu-py37": "cu110-ubuntu18.04-v8",
            "tensorflow-1.15.4-gpu-py37": "cu110-ubuntu18.04",
            "tensorflow-1.15.5-gpu-py37": "cu110-ubuntu18.04",
            "mxnet-1.8-gpu-py37": "cu110-ubuntu16.04-v1",
            "mxnet-1.8.0-gpu-py37": "cu110-ubuntu16.04",
            "pytorch-1.6-gpu-py36": "cu110-ubuntu18.04-v3",
            "pytorch-1.6.0-gpu-py36": "cu110-ubuntu18.04",
            "pytorch-1.6-gpu-py3": "cu110-ubuntu18.04-v3",
            "pytorch-1.6.0-gpu-py3": "cu110-ubuntu18.04",
        }
        key = "-".join([framework, tag])
        if key in container_versions:
            tag = "-".join([tag, container_versions[key]])

    if tag:
        repo += ":{}".format(tag)

    return ECR_URI_TEMPLATE.format(registry=registry, hostname=hostname, repository=repo)


def _config_for_framework_and_scope(framework, image_scope, accelerator_type=None):
    """Loads the JSON config for the given framework and image scope."""
    config = config_for_framework(framework)

    if accelerator_type:
        _validate_accelerator_type(accelerator_type)

        if image_scope not in ("eia", "inference"):
            logger.warning(
                "Elastic inference is for inference only. Ignoring image scope: %s.", image_scope
            )
        image_scope = "eia"

    available_scopes = config.get("scope", config.keys())

    if len(available_scopes) == 1:
        if image_scope and image_scope != list(available_scopes)[0]:
            logger.warning(
                "Defaulting to only supported image scope: %s. Ignoring image scope: %s.",
                available_scopes[0],
                image_scope,
            )
        image_scope = list(available_scopes)[0]

    if not image_scope and "scope" in config and set(available_scopes) == {"training", "inference"}:
        logger.info(
            "Same images used for training and inference. Defaulting to image scope: %s.",
            available_scopes[0],
        )
        image_scope = available_scopes[0]

    _validate_arg(image_scope, available_scopes, "image scope")
    return config if "scope" in config else config[image_scope]


def config_for_framework(framework):
    """Loads the JSON config for the given framework."""
    fname = os.path.join(os.path.dirname(__file__), "image_uri_config", "{}.json".format(framework))
    with open(fname) as f:
        return json.load(f)


def _validate_accelerator_type(accelerator_type):
    """Raises a ``ValueError`` if ``accelerator_type`` is invalid."""
    if not accelerator_type.startswith("ml.eia") and accelerator_type != "local_sagemaker_notebook":
        raise ValueError(
            "Invalid SageMaker Elastic Inference accelerator type: {}. "
            "See https://docs.aws.amazon.com/sagemaker/latest/dg/ei.html".format(accelerator_type)
        )


def _validate_version_and_set_if_needed(version, config, framework):
    """Checks if the framework/algorithm version is one of the supported versions."""
    available_versions = list(config["versions"].keys())
    aliased_versions = list(config.get("version_aliases", {}).keys())

    if len(available_versions) == 1 and version not in aliased_versions:
        log_message = "Defaulting to the only supported framework/algorithm version: {}.".format(
            available_versions[0]
        )
        if version and version != available_versions[0]:
            logger.warning("%s Ignoring framework/algorithm version: %s.", log_message, version)
        elif not version:
            logger.info(log_message)

        return available_versions[0]

    _validate_arg(version, available_versions + aliased_versions, "{} version".format(framework))
    return version


def _version_for_config(version, config):
    """Returns the version string for retrieving a framework version's specific config."""
    if "version_aliases" in config:
        if version in config["version_aliases"].keys():
            return config["version_aliases"][version]

    return version


def _registry_from_region(region, registry_dict):
    """Returns the ECR registry (AWS account number) for the given region."""
    _validate_arg(region, registry_dict.keys(), "region")
    return registry_dict[region]


def _processor(instance_type, available_processors):
    """Returns the processor type for the given instance type."""
    if not available_processors:
        logger.info("Ignoring unnecessary instance type: %s.", instance_type)
        return None

    if len(available_processors) == 1 and not instance_type:
        logger.info("Defaulting to only supported image scope: %s.", available_processors[0])
        return available_processors[0]

    if not instance_type:
        raise ValueError(
            "Empty SageMaker instance type. For options, see: "
            "https://aws.amazon.com/sagemaker/pricing/instance-types"
        )

    if instance_type.startswith("local"):
        processor = "cpu" if instance_type == "local" else "gpu"
    else:
        # looks for either "ml.<family>.<size>" or "ml_<family>"
        match = re.match(r"^ml[\._]([a-z\d]+)\.?\w*$", instance_type)
        if match:
            family = match[1]

            # For some frameworks, we have optimized images for specific families, e.g c5 or p3.
            # In those cases, we use the family name in the image tag. In other cases, we use
            # 'cpu' or 'gpu'.
            if family in available_processors:
                processor = family
            elif family.startswith("inf"):
                processor = "inf"
            elif family[0] in ("g", "p"):
                processor = "gpu"
            else:
                processor = "cpu"
        else:
            raise ValueError(
                "Invalid SageMaker instance type: {}. For options, see: "
                "https://aws.amazon.com/sagemaker/pricing/instance-types".format(instance_type)
            )

    _validate_arg(processor, available_processors, "processor")
    return processor


def _should_auto_select_container_version(instance_type, distribution):
    """Returns a boolean that indicates whether to use an auto-selected container version."""
    p4d = False
    if instance_type:
        # looks for either "ml.<family>.<size>" or "ml_<family>"
        match = re.match(r"^ml[\._]([a-z\d]+)\.?\w*$", instance_type)
        if match:
            family = match[1]
            p4d = family == "p4d"

    smdistributed = False
    if distribution:
        smdistributed = "smdistributed" in distribution

    return p4d or smdistributed


def _validate_py_version_and_set_if_needed(py_version, version_config, framework):
    """Checks if the Python version is one of the supported versions."""
    if "repository" in version_config:
        available_versions = version_config.get("py_versions")
    else:
        available_versions = list(version_config.keys())

    if not available_versions:
        if py_version:
            logger.info("Ignoring unnecessary Python version: %s.", py_version)
        return None

    if py_version is None and defaults.SPARK_NAME == framework:
        return None

    if py_version is None and len(available_versions) == 1:
        logger.info("Defaulting to only available Python version: %s", available_versions[0])
        return available_versions[0]

    _validate_arg(py_version, available_versions, "Python version")
    return py_version


def _validate_arg(arg, available_options, arg_name):
    """Checks if the arg is in the available options, and raises a ``ValueError`` if not."""
    if arg not in available_options:
        raise ValueError(
            "Unsupported {arg_name}: {arg}. You may need to upgrade your SDK version "
            "(pip install -U sagemaker) for newer {arg_name}s. Supported {arg_name}(s): "
            "{options}.".format(arg_name=arg_name, arg=arg, options=", ".join(available_options))
        )


def _format_tag(tag_prefix, processor, py_version, container_version):
    """Creates a tag for the image URI."""
    return "-".join(x for x in (tag_prefix, processor, py_version, container_version) if x)
