import os
import re
import shlex
import sys
from concurrent.futures import TimeoutError, as_completed
from contextlib import ExitStack
from glob import glob
from pathlib import Path
from typing import Any, List, Optional, Set, Tuple

import typer
from mypy import api
from pebble import ProcessPool
from rich.progress import MofNCompleteColumn, Progress, TimeElapsedColumn
from typer import Typer, echo

FILE_PATTERN = re.compile(r"^([^:]*.py).*")

app = Typer(
    help="Progressive type annotation without regression! 🚀", add_completion=False
)


def write_to_file(file: Optional[str], filenames: Set[str]) -> None:
    """Write filenames to file or stdout.

    Args:
        file (Optional[str]): File to write to. If None, write to stdout.
        filenames (Set[str]): Filenames to write into the file.
    """
    output_filenames = "\n".join(sorted(filenames))
    with ExitStack() as stack:
        fh = stack.enter_context(open(file, "w")) if file else sys.stdout
        echo(output_filenames, file=fh)


def call_mypy_api(args: list[str]) -> Tuple[str, Any]:
    return args[0], api.run([*args])


@app.command()
def dump(
    directory: str = ".",
    mypy_args: Optional[str] = None,
    timeout: int = 30,
    exclude: Optional[List[str]] = None,
    output: Optional[str] = None,
) -> None:
    """Generate a list of files that are not fully type annotated."""
    exclude = exclude or []
    filenames: List[str] = []
    bad_filenames: Set[str] = set()
    processedFiles = set()
    args = shlex.split(mypy_args or "")
    if directory == ".":
        directory = os.getcwd()  # pragma: no cover
    path = str(Path(directory)) + "/**/*.py"

    for filename in glob(path, recursive=True):
        pure_filename = filename[len(directory) + 1 :]
        if any(pure_filename.startswith(f"{name}") for name in exclude):
            continue  # pragma: no cover
        filenames.append(pure_filename)

    with Progress(
        *Progress.get_default_columns(),
        TimeElapsedColumn(),
        MofNCompleteColumn(),
    ) as progress, ProcessPool() as pool:
        total = len(filenames)
        task = progress.add_task("Running mypy...", total=total)

        futures = [
            pool.submit(call_mypy_api, timeout=timeout, args=[filename, *args])
            for filename in filenames
        ]
        for future in as_completed(futures):
            try:
                filename, api_result = future.result()
                _, _, exit_code = api_result
                include_filename = bool(exit_code)
                processedFiles.add(filename)
            except TimeoutError:  # pragma: no cover
                progress.console.print("TimeoutError")
                include_filename = True
            except Exception as e:  # pragma: no cover
                progress.console.print("Exception: ", e)
            if include_filename:
                bad_filenames.add(filename)
            progress.advance(task)
        files_resulted_in_exception = set(filenames) - processedFiles
        if files_resulted_in_exception:
            echo(f"Exception files : {set(filenames) - processedFiles}")

    write_to_file(output, bad_filenames)


@app.command(help="Check the given files with mypy, applying a set of custom rules.")
def check(
    files: List[str],
    ignore_file: Path = typer.Option(..., "--ignore-file", "-f"),
    mypy_args: Optional[str] = None,
    timeout: Optional[int] = 40,
) -> None:
    """Check the given files with mypy, applying a set of custom rules.

    Given the input `files`, and the list of files in the `ignore_file`:
    - If a file is in the list, and is fully type annotated, it will be removed from the list.
    - If a file is in the list, and is not fully annotated, it will be ignored.
    - If a file is not in the list, and is fully annotated, it will be ignored.
    - If a file is not in the list, and is not fully annotated, it will raise errors.
    """
    args = shlex.split(mypy_args or "")
    with ignore_file.open("r") as file:
        all_ignored_files = {line.strip() for line in file.readlines()}
    files_to_ignore = set(files) & all_ignored_files

    files_with_error = set()
    processedFiles = set()

    with Progress(
        *Progress.get_default_columns(),
        TimeElapsedColumn(),
        MofNCompleteColumn(),
    ) as progress, ProcessPool() as pool:
        total = len(files)
        task = progress.add_task("Checking mypy on files...", total=total)
        futures = [
            pool.submit(call_mypy_api, args=[filename, *args], timeout=timeout)
            for filename in files
        ]

        output = []
        for future in as_completed(futures):
            try:
                filename, api_result = future.result()
                result, _, exit_code = api_result
                processedFiles.add(filename)
                if exit_code == 2:
                    output.append(f"Error in file: {filename}")
                    files_with_error.add(filename)
                    print(
                        "promypy failed with exit status code 2. Please raise this issue with the developer."
                    )
                if exit_code != 0:
                    for line in result.split("\n"):
                        match = re.match(FILE_PATTERN, line)
                        if ":" in line and match:
                            filename = match.group(1)
                            files_with_error.add(filename)
                            if filename not in files_to_ignore:
                                output.append(line)
            except TimeoutError:  # pragma: no cover
                progress.console.print("TimeoutError")
            except Exception as e:  # pragma: no cover
                progress.console.print("Exception: ", e)
            progress.advance(task)
        files_resulted_in_exception = set(files) - processedFiles
        if files_resulted_in_exception:
            echo(f"Exception files : {set(files) - processedFiles}")

    files_to_remove = files_to_ignore - files_with_error
    ignored_files = all_ignored_files - files_to_remove

    modify_ignored_files = all_ignored_files > ignored_files
    if modify_ignored_files and len(ignored_files) != 0:
        echo(f"{ignore_file} has been updated.")
        with ignore_file.open("w") as file:
            echo("\n".join(sorted(ignored_files)), file=file)

    for line in output:
        echo(line)

    if len(output) == 0 and len(ignored_files) == 0:
        echo("This project is now fully type annotated! 🎉")
        ignore_file.unlink()
        raise typer.Exit(1)

    if len(output) == 0:
        echo("Success! 🚀")
        echo(f"Number of files missing: {len(ignored_files)}.")
        raise typer.Exit(1 if modify_ignored_files else 0)

    raise typer.Exit(exit_code)


if __name__ == "__main__":  # pragma: no cover
    app()
