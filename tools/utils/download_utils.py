import os
import re
import tempfile
import threading
from collections.abc import Generator
from concurrent.futures import Future
from functools import cached_property
from pathlib import Path
from typing import Optional, Mapping, Any
from urllib.parse import urlparse, unquote

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from httpx import Response, Client, Limits
from yarl import URL

from tools.utils.file_utils import force_delete_path
from tools.utils.tool_utils import send_text_in_chunks


class ClientHolder:
    default_connection_limits: Limits

    def __init__(self):
        self.default_connection_limits = Limits(
            max_connections=200,
            max_keepalive_connections=50,
            keepalive_expiry=180)
        pass

    @cached_property
    def default_client(self) -> Client:
        return Client(
            http2=True,
            follow_redirects=True,
            verify=True,
            default_encoding="utf-8",
            proxy=None,
            limits=self.default_connection_limits,
        )

    @cached_property
    def default_client_without_ssl_verify(self) -> Client:
        return Client(
            http2=True,
            follow_redirects=True,
            verify=True,
            default_encoding="utf-8",
            proxy=None,
            limits=self.default_connection_limits,
        )

    def get_client(self, proxy_url: Optional[str], ssl_certificate_verify: bool) -> tuple[Client, bool]:
        """
        :param proxy_url:
        :param ssl_certificate_verify:
        :return:
            Client: httpx client
            bool: whether should be manually closed after use
        """
        if proxy_url:
            return Client(
                http2=True,
                follow_redirects=True,
                verify=ssl_certificate_verify,
                default_encoding="utf-8",
                proxy=proxy_url,
            ), True
        elif not ssl_certificate_verify:
            return self.default_client_without_ssl_verify, False
        else:
            # should not be closed manually after use as its shared default client
            return self.default_client, False


client_holder = ClientHolder()


def patch_request_headers(http_headers: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    headers = http_headers or {}

    # set default Accept-Encoding request header if not provided
    if not headers.get("Accept-Encoding"):
        headers["Accept-Encoding"] = "gzip, deflate, br, zstd"

    # enable keep-alive by default
    if not headers.get("Connection"):
        headers["Connection"] = "keep-alive"

    return headers


def download_to_temp(method: str, url: str,
                     timeout: float = 5.0,
                     ssl_certificate_verify: bool = True,
                     request_headers: Mapping[str, str] = None,
                     request_body: Optional[str] = None,
                     proxy_url: Optional[str] = None,
                     cancel_event: threading.Event = None,
                     custom_filename: Optional[str] = None,
                     idx: int = 0,
                     ) -> tuple[int, Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Download a file to a temporary file,
    and return the file path, MIME type, and file name.
    """""
    client, should_close_client = client_holder.get_client(proxy_url, ssl_certificate_verify)
    request_headers = patch_request_headers(request_headers)
    try:
        with client.stream(
                method=method,
                url=url,
                headers=request_headers,
                timeout=timeout,
                content=request_body.encode("utf-8") if request_body else None,
        ) as response:
            try:
                response.raise_for_status()
            except Exception as e:
                raise ValueError(
                    f"Failed to download file from {url}, HTTP status code: {response.status_code}, error: {e}")

            # check if the download is cancelled
            if cancel_event and cancel_event.is_set():
                return idx, None, None, None, None

            content_type = response.headers.get('content-type')
            mime_type: Optional[str] = content_type.split(';')[0].strip() if content_type else None
            encoding = response.encoding
            filename = custom_filename or guess_file_name(url, response)

            with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                file_path = temp_file.name
                try:
                    # Stream the response content to the temporary file
                    for chunk in response.iter_bytes(chunk_size=8192):
                        # check if the download is cancelled
                        if cancel_event and cancel_event.is_set():
                            return idx, None, None, None, None

                        temp_file.write(chunk)
                except:
                    force_delete_path(file_path)
                    file_path = None
                finally:
                    temp_file.close()

        return idx, file_path, mime_type, filename, encoding
    finally:
        if should_close_client:
            client.close()


def guess_file_name(url: str, response: Response) -> Optional[str]:
    filename = None

    # Get filename from possible Content-Disposition header
    content_disposition = response.headers.get('content-disposition', '')
    if content_disposition:
        # Expected value：filename="example.txt"
        match = re.search(r'filename\*?=["\']?(?:.*\')?([^"\';]+)["\']?', content_disposition, re.IGNORECASE)
        if match:
            filename = unquote(match.group(1).strip())

    # Parsing URL to get the filename if not found in headers
    if not filename:
        parsed = urlparse(url)
        path = parsed.path
        if path:
            filename = unquote(os.path.basename(path))

    return filename


def parse_url(url: str) -> Optional[URL]:
    """
    Parse a URL and return a yarl.URL object or None if parsing fails.
    """
    try:
        return URL(url)
    except ValueError as e:
        return None


def handle_partial_done(cancel_event: threading.Event,
                        done: set[Future[Any]],
                        not_done: set[Future[Any]],
                        ):
    # cancel all downloads by setting the cancel event
    assert not cancel_event.is_set()
    cancel_event.set()

    # cancel unfinished futures
    for f in not_done:
        f.cancel()
    done_without_exception = [f for f in done if not f.exception()]
    done_with_exception = [f for f in done if f.exception()]
    # Clean up the downloaded temporary files for those that completed without exceptions
    for future in done_without_exception:
        file_path, _, _, _ = future.result()
        force_delete_path(file_path)
    # Raise the first exception encountered in the futures
    for future in done_with_exception:
        if future.exception():
            raise future.exception()


def handle_all_done(tool: Tool,
                    done: set[Future[Any]],
                    is_to_file: bool = True,
                    ) -> Generator[ToolInvokeMessage, None, None]:
    # all completed without exceptions
    sorted_done = sorted(done, key=lambda f: f.result()[0])  # sort by index
    for future in sorted_done:
        idx, file_path, mime_type, filename, encoding = future.result()
        try:
            if is_to_file:
                downloaded_file_bytes = Path(file_path).read_bytes()
                yield tool.create_blob_message(
                    blob=downloaded_file_bytes,
                    meta={
                        "mime_type": mime_type,
                        "filename": filename,
                    }
                )
            else:
                downloaded_file_text = Path(file_path).read_text(encoding=encoding or "utf-8")
                yield from send_text_in_chunks(tool, text=downloaded_file_text)
        finally:
            # Clean up the downloaded temporary files
            force_delete_path(file_path)
