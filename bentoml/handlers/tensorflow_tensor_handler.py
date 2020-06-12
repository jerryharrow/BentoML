# Copyright 2019 Atalaya Tech, Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

# http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Iterable

import json
import argparse
from bentoml.handlers.utils import (
    NestedConverter,
    tf_b64_2_bytes,
    tf_tensor_2_serializable,
    concat_list,
)
from bentoml.handlers.base_handlers import BentoHandler
from bentoml.exceptions import BentoMLException, BadInput
from bentoml.marshal.utils import SimpleResponse, SimpleRequest


decode_b64_if_needed = NestedConverter(tf_b64_2_bytes)
decode_tf_if_needed = NestedConverter(tf_tensor_2_serializable)


class TensorflowTensorHandler(BentoHandler):
    """
    Tensor handlers for Tensorflow models.
    Transform incoming tf tensor data from http request, cli or lambda event into
    tf tensor.
    The behaviour should be compatible with tensorflow serving REST API:
    * https://www.tensorflow.org/tfx/serving/api_rest#classify_and_regress_api
    * https://www.tensorflow.org/tfx/serving/api_rest#predict_api

    Args:
        * method: equivalence of serving API methods: (predict, classify, regress)

    Raises:
        BentoMLException: BentoML currently doesn't support Content-Type
    """

    BATCH_MODE_SUPPORTED = True
    METHODS = (PREDICT, CLASSIFY, REGRESS) = ("predict", "classify", "regress")

    def __init__(self, method=PREDICT, is_batch_input=True, **base_kwargs):
        super(TensorflowTensorHandler, self).__init__(
            is_batch_input=is_batch_input, **base_kwargs
        )
        self.method = method

    @property
    def config(self):
        base_config = super(TensorflowTensorHandler, self).config
        return dict(base_config, method=self.method,)

    @property
    def request_schema(self):
        if self.method == self.PREDICT:
            return {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "signature_name": {"type": "string", "default": None},
                            "instances": {
                                "type": "array",
                                "items": {"type": "object"},
                                "default": None,
                            },
                            "inputs": {"type": "object", "default": None},
                        },
                    }
                }
            }
        else:
            raise NotImplementedError(f"method {self.method} is not implemented")

    def _handle_raw_str(self, raw_str, func):
        import tensorflow as tf

        parsed_json = json.loads(raw_str)
        if parsed_json.get("instances") is not None:
            instances = parsed_json.get("instances")
            instances = decode_b64_if_needed(instances)
            parsed_tensor = tf.constant(instances)

        elif parsed_json.get("inputs"):
            raise NotImplementedError("column format 'inputs' is not implemented")

        return func(parsed_tensor)

    def handle_batch_request(
        self, requests: Iterable[SimpleRequest], func
    ) -> Iterable[SimpleResponse]:
        """
        TODO(hrmthw):
        1. specify batch dim
        1. output str fromat
        """
        import tensorflow as tf

        bad_resp = SimpleResponse(400, None, "input format error")
        instances_list = [None] * len(requests)
        responses = [bad_resp] * len(requests)
        batch_flags = [None] * len(requests)

        for i, request in enumerate(requests):
            try:
                raw_str = request.data
                batch_flags[i] = (
                    request.formated_headers.get(
                        self._BATCH_REQUEST_HEADER.lower(),
                        "true" if self.config.get("is_batch_input") else "false",
                    )
                    == "true"
                )
                parsed_json = json.loads(raw_str)
                if parsed_json.get("instances") is not None:
                    instances = parsed_json.get("instances")
                    if instances is None:
                        continue
                    instances = decode_b64_if_needed(instances)
                    instances_list[i] = instances

                elif parsed_json.get("inputs"):
                    responses[i] = SimpleResponse(
                        501, None, "Column format 'inputs' not implemented"
                    )

            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            except Exception:  # pylint: disable=broad-except
                import traceback

                err = traceback.format_exc()
                responses[i] = SimpleResponse(
                    500, None, f"Internal Server Error: {err}"
                )

        merged_instances, slices = concat_list(instances_list, batch_flags=batch_flags)

        parsed_tensor = tf.constant(merged_instances)
        merged_result = func(parsed_tensor)
        return self.output_adapter.to_batch_response(
            merged_result, slices=slices, fallbacks=responses, requests=requests
        )

    def handle_request(self, request, func):
        """Handle http request that has jsonlized tensorflow tensor. It will convert it
        into a tf tensor for the function to consume.

        Args:
            request: incoming request object.
            func: function that will take ndarray as its arg.
        Return:
            response object
        """
        if request.content_type == "application/json":
            input_str = request.data.decode("utf-8")
            result = self._handle_raw_str(input_str, func)
            return self.output_adapter.to_response(result, request)
        else:
            raise BadInput(
                "Request content-type must be 'application/json'"
                " for this BentoService API"
            )

    def handle_cli(self, args, func):
        parser = argparse.ArgumentParser()
        parser.add_argument("--input", required=True)
        parsed_args, unknown_args = parser.parse_known_args(args)

        result = self._handle_raw_str(parsed_args.input, func)
        return self.output_adapter.to_cli(result, unknown_args)

    def handle_aws_lambda_event(self, event, func):
        if event["headers"].get("Content-Type", "") == "application/json":
            result = self._handle_raw_str(event["body"], func)
            return self.output_adapter.to_aws_lambda_event(result, event)
        else:
            raise BentoMLException(
                "BentoML currently doesn't support Content-Type: {content_type} for "
                "AWS Lambda".format(content_type=event["headers"]["Content-Type"])
            )
