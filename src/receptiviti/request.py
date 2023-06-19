"""Make requests to the API."""

from typing import Union, List
import os
from time import perf_counter, sleep
from multiprocessing import Pool, cpu_count
import sys
import re
import hashlib
import requests
import numpy
import pandas
from .status import status
from .readin_env import readin_env


def _process(bundle: pandas.DataFrame, ops: dict) -> Union[pandas.DataFrame, None]:
    body = [
        {"content": text, "request_id": hashlib.md5(text.encode()).hexdigest(), **ops["add"]}
        for text in bundle["text"]
    ]
    res = requests.post(ops["url"], auth=ops["auth"], json=body, timeout=9999)
    content = None
    if res.status_code == 200:
        content = pandas.json_normalize(res.json()["results"])
    elif ops["retries"] > 0:
        sleep(1)
        ops["retries"] -= 1
        print(res.text)
        _process(bundle, ops)
    return content


def request(
    text: Union[str, list, pandas.DataFrame],
    output: Union[str, None] = None,
    ids: Union[str, List[Union[str, int]], None] = None,
    text_column: Union[str, None] = None,
    id_column: Union[str, None] = None,
    api_args: Union[dict, None] = None,
    frameworks: Union[str, List[str], None] = None,
    framework_prefix: Union[bool, None] = None,
    bundle_size=1000,
    bundle_byte_limit=75e5,
    retry_limit=50,
    cores=cpu_count() - 2,
    verbose=False,
    overwrite=False,
    dotenv: Union[bool, str] = True,
    key=os.getenv("RECEPTIVITI_KEY", ""),
    secret=os.getenv("RECEPTIVITI_SECRET", ""),
    url=os.getenv("RECEPTIVITI_URL", ""),
    version=os.getenv("RECEPTIVITI_VERSION", ""),
    endpoint=os.getenv("RECEPTIVITI_ENDPOINT", ""),
) -> pandas.DataFrame:
    """
    Send texts to be scored by the API.

    Args:
      text (str | list | pandas.DataFrame): Text to be processed.
      output (str): Path to a file to write results to.
      ids (str | list): Vector of IDs for each `text`, or a column name in `text` containing IDs.
      text_column (str): Column name in `text` containing text.
      id_column (str): Column name in `text` containing IDs.
      api_args (dict): Additional arguments to include in the request.
      frameworks (str | list): One or more names of frameworks to return.
      framework_prefix (bool): If `False`, will drop framework prefix from column names.
        If one framework is selected, will default to `False`.
      bundle_size (int): Maximum number of texts per bundle.
      bundle_byte_limit (float): Maximum byte size of each bundle.
      retry_limit (int): Number of times to retry a failed request.
      cores (int): Number of CPU cores to use.
      verbose (bool): If `False`, will not print status messages.
      overwrite (bool): If `True`, will overwrite an existing `output` file.
      dotenv (bool | str): Path to a .env file to read environment variables from. By default,
        will for a file in the current directory or `~/Documents`. Passed to `readin_env` as `path`.
      key (str): Your API key.
      secret (str): Your API secret.
      url (str): The URL of the API; defaults to `https://api.receptiviti.com`.
      version (str): Version of the API; defaults to `v1`.
      endpoint (str): Endpoint of the API; defaults to `framework`.

    Returns:
      Scores associated with each input text.
    """
    if output is not None and os.path.isfile(output) and not overwrite:
        raise RuntimeError("`output` file already exists; use `overwrite=True` to overwrite it")
    start_time = perf_counter()

    # resolve credentials and check status
    if dotenv:
        readin_env("." if isinstance(dotenv, bool) else dotenv)
    if url == "":
        url = os.getenv("RECEPTIVITI_URL", "https://api.receptiviti.com")
    url = ("https://" if re.match("http", url, re.I) is None else "") + re.sub(
        "/[Vv]\\d(?:/.*)?$|/+$", "", url
    )
    if key == "":
        key = os.getenv("RECEPTIVITI_KEY", "")
    if secret == "":
        secret = os.getenv("RECEPTIVITI_SECRET", "")
    if version == "":
        version = os.getenv("RECEPTIVITI_VERSION", "v1")
    if endpoint == "":
        endpoint = os.getenv("RECEPTIVITI_ENDPOINT", "framework")
    api_status = status(url, key, secret, dotenv, verbose=False)
    if api_status.status_code != 200:
        raise RuntimeError(f"API status failed: {api_status.status_code}")

    # resolve text and ids
    if isinstance(text, str) and os.path.isfile(text):
        if verbose:
            print(f"reading in texts from a file ({perf_counter() - start_time:.4f})")
        text = pandas.read_csv(text)
    if isinstance(text, pandas.DataFrame):
        if id_column is not None:
            if id_column in text:
                ids = text[id_column].to_list()
            else:
                raise IndexError(f"`id_column` ({id_column}) is not in `text`")
        if text_column is not None:
            if text_column in text:
                text = text[text_column].to_list()
            else:
                raise IndexError(f"`text_column` ({text_column}) is not in `text`")
        else:
            raise RuntimeError("`text` is a DataFrame, but no `text_column` is specified")
    if isinstance(text, str):
        text = [text]
    n_texts = len(text)
    if ids is None:
        ids = numpy.arange(1, n_texts + 1)
    elif len(ids) != n_texts:
        raise RuntimeError("`ids` is not the same length as `text`")

    # prepare bundles
    if verbose:
        print(f"preparing text ({perf_counter() - start_time:.4f})")
    data = pandas.DataFrame({"text": text, "id": ids})
    data = data[(~data.duplicated(subset=["text"])) | (data["text"] == "") | (data["text"].isna())]
    if not data.ndim:
        raise RuntimeError("no valid texts to process")
    n_bundles = n_texts / min(1000, max(1, bundle_size))
    groups = data.groupby(
        numpy.tile(numpy.arange(n_bundles + 1), n_texts)[:n_texts], group_keys=False
    )
    bundles = []
    for _, group in groups:
        if sys.getsizeof(group) > bundle_byte_limit:
            start = current = end = 0
            for txt in group["text"]:
                size = sys.getsizeof(txt)
                if size > bundle_byte_limit:
                    raise RuntimeError(
                        "one of your texts is over the bundle size"
                        + f" limit ({bundle_byte_limit / 1e6} MB)"
                    )
                if (current + size) > bundle_byte_limit:
                    bundles.append(group[start:end])
                    start = end = end + 1
                    current = size
                else:
                    end += 1
                    current += size
            bundles.append(group[start:])
        else:
            bundles.append(group)
    if verbose:
        print(
            f"prepared text in {len(bundles)} {'bundles' if len(bundles) > 1 else 'bundle'}",
            f"({perf_counter() - start_time:.4f})",
        )

    # process bundles
    args = {
        "url": f"{url}/{version}/{endpoint}/bulk",
        "auth": (key, secret),
        "retries": retry_limit,
        "add": {} if api_args is None else api_args,
    }

    if cores > 1:
        with Pool(cores) as pool:
            res = pool.starmap_async(_process, [(b, args) for b in bundles]).get()
    else:
        res = [_process(b, args) for b in bundles]
    res = pandas.concat(res, ignore_index=True, sort=False)

    # finalize
    if output is not None:
        if verbose:
            print(f"writing results to file: {output} ({perf_counter() - start_time:.4f})")
        res.to_csv(output, index=False)

    res = res.drop(
        filter(
            lambda col: col in res.columns,
            ["response_id", "language", "version", "error", "custom"],
        ),
        axis="columns",
    )
    if frameworks is not None:
        if verbose:
            print(f"selecting frameworks ({perf_counter() - start_time:.4f})")
        if isinstance(frameworks, str) or len(frameworks) == 1:
            if framework_prefix is None:
                framework_prefix = False
            frameworks = [frameworks]
        select = ["request_id", *frameworks]
        res = res.filter(regex=f"^(?:{'|'.join(select)})(?:$|\\.)")
    if isinstance(framework_prefix, bool) and not framework_prefix:
        prefix_pattern = re.compile("^[^.]+\\.")
        res.columns = [prefix_pattern.sub("", col) for col in res.columns]

    if verbose:
        print(f"done ({perf_counter() - start_time:.4f})")

    return res
