from pathlib import Path

import onnx
from onnx import TensorProto, helper

from ml.export.onnx import read_json_metadata, write_json_metadata


class TestWriteJsonMetadata:
    def test_replaces_the_model_atomically(self, tmp_path: Path):
        model_path = tmp_path / "model.onnx"
        input_info = helper.make_tensor_value_info("input", TensorProto.FLOAT, (1, 1))
        output_info = helper.make_tensor_value_info("output", TensorProto.FLOAT, (1, 1))
        model = helper.make_model(
            helper.make_graph(
                (helper.make_node("Identity", ("input",), ("output",)),),
                "identity",
                (input_info,),
                (output_info,),
            )
        )
        onnx.save(model, model_path, save_as_external_data=False)
        original_inode = model_path.stat().st_ino

        write_json_metadata(model_path, key="test.metadata", value={"value": 1})

        assert model_path.stat().st_ino != original_inode
        assert read_json_metadata(model_path, key="test.metadata") == {"value": 1}
