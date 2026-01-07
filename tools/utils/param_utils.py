import json
from dataclasses import dataclass
from typing import Mapping, Optional, Any

from httpx import URL

from tools.utils.download_utils import parse_url


@dataclass
class CommonPrams(object):
    urls: list[URL]
    request_method: str
    request_timeout: float
    request_headers: Optional[Mapping[str, str]]
    request_body_str: Optional[str]
    ssl_certificate_verify: bool
    proxy_url: Optional[str]
    custom_output_filenames: list[str]

    def __init__(self):
        self.urls = []
        self.request_method = "GET"
        self.request_timeout = 5.0
        self.request_headers = None
        self.request_body_str = None
        self.ssl_certificate_verify = True
        self.proxy_url = None


def parse_common_params(tool_parameters: dict[str, Any]) -> CommonPrams:
    parsed_params = CommonPrams()
    parsed_params.urls = [parse_url(s) for s in tool_parameters.get("url", "").split("\n") if s and parse_url(s)]
    parsed_params.request_method = tool_parameters.get("request_method", "GET")
    parsed_params.request_timeout = float(tool_parameters.get("request_timeout", "5"))
    parsed_params.request_headers = parse_json_string_dict(tool_parameters.get("request_headers"))
    parsed_params.request_body_str = tool_parameters.get("request_body_str")
    parsed_params.ssl_certificate_verify = tool_parameters.get("ssl_certificate_verify", "false") == "true"
    parsed_params.proxy_url = tool_parameters.get("proxy_url")
    parsed_params.custom_output_filenames = tool_parameters.get("output_filename", "").split("\n")

    return parsed_params


def parse_json_string_dict(json_str: str) -> Mapping[str, str]:
    if not json_str:
        return {}

    try:
        data = json.loads(json_str)
    except:
        raise ValueError("invalid json")
    if not isinstance(data, dict):
        raise ValueError("Input JSON string must represent a dictionary.")

    str_dict: Mapping[str, str] = {key: str(value) for key, value in data.items()}
    return str_dict
