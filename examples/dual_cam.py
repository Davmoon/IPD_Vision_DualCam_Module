import degirum as dg
import degirum_tools
import degirum_tools.streams as dgstreams

# inference_host_address = "@cloud"
inference_host_address = "@local"

# choose zoo_url
#zoo_url = "degirum/models_hailort"
zoo_url = "../models"

# set token
#token = degirum_tools.get_token()
token = '' # leave empty for local inference

# Define the configurations for video file and webcam
configurations = [
    {
        "model_name": "yolov8n_relu6_coco--640x640_quant_hailort_hailo8l_1",
        "source": "../assets/Traffic.mp4",  # Video file
        "display_name": "Traffic Camera",
    },
    {
        "model_name": "yolov8n_relu6_coco--640x640_quant_hailort_hailo8l_1",
        "source": "/base/axi/pcie@1000120000/rp1/i2c@80000/imx708@1a",  # Webcam index
        "display_name": "Webcam Feed",
    },
]


# load models
models = [
    dg.load_model(cfg["model_name"], inference_host_address, zoo_url, token)
    for cfg in configurations
]

# define gizmos
sources = [dgstreams.VideoSourceGizmo(cfg["source"]) for cfg in configurations]
detectors = [dgstreams.AiSimpleGizmo(model) for model in models]
display = dgstreams.VideoDisplayGizmo(
    [cfg["display_name"] for cfg in configurations], show_ai_overlay=True, show_fps=True
)

# create pipeline
pipeline = (
    (source >> detector for source, detector in zip(sources, detectors)),
    (detector >> display[di] for di, detector in enumerate(detectors)),
)

# start composition
dgstreams.Composition(*pipeline).start()