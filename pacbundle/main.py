import os
import re
import shlex
import subprocess
from collections import Counter
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Iterable

import tomllib
import typer
from pydantic import BaseModel, Field, ValidationError, field_validator
from rich import print
from rich.columns import Columns
from rich.console import Console
from rich.table import Table
from typing_extensions import Annotated

app = typer.Typer()
console = Console()
state = {"verbose": False, "dry_run": False}

APP_NAME = "pacbundle"


class Bundle(BaseModel):
    members: list[str]
    include: str | None = None

    @field_validator("members")
    def members_are_valid_identifiers(cls, members: list[str]):
        for member in members:
            pattern = R"^(g#|#)?[\w@.+-]+$"
            if not re.match(pattern, member):
                raise ValueError(f"Invalid identifier: {member}")
        return members

    @cached_property
    def is_included(self):
        return (
            self.include is not None
            and subprocess.run(self.include, shell=True, check=False).returncode == 0
        )


class Settings(BaseModel):
    install_command: str = "sudo pacman -S"


class Config(BaseModel):
    bundles: dict[str, Bundle] = Field(default_factory=dict)
    settings: Settings = Field(default=Settings())


def identifier_type(identifier: str):
    if identifier.startswith("#"):
        return "bundle"
    if identifier.startswith("g#"):
        return "group"
    return "package"


app_dir = Path(typer.get_app_dir(APP_NAME))
config_path = app_dir / "config.toml"


@lru_cache(1)
def read_config() -> Config:
    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
            return Config.model_validate(data)
    except FileNotFoundError:
        print("[red]Cannot find config file, exiting.[/red]")
        raise typer.Exit(1)
    except ValidationError as e:
        print(f"[red]The config file is invalid:\n{e}[/red]")
        raise typer.Exit(1)
    except Exception:
        raise Exception


def pacman(*args: str) -> list[str]:
    output = (
        subprocess.run(["pacman", *args], capture_output=True, check=True)
        .stdout.decode()
        .split("\n")
    )
    return [line for line in output if line]


def run_action(*args: str) -> None:
    if state["dry_run"] or state["verbose"]:
        print(f"Running command: [italic]{shlex.join(args)}[/italic]")
    if not state["dry_run"]:
        subprocess.run(args, check=True)


@lru_cache(1)
def pacman_groups() -> dict[str, set[str]]:
    group_output = pacman("-Qg")
    groups: dict[str, set[str]] = {}
    for line in group_output:
        group_name, package_name = line.split()
        if group_name not in groups:
            groups[group_name] = set()
        groups[group_name].add(package_name)
    return groups


@lru_cache(1)
def get_explicitly_installed_packages() -> set[str]:
    return set(line.split(" ")[0] for line in pacman("-Qe"))


@lru_cache(1)
def get_installed_packages() -> set[str]:
    return set(line.split(" ")[0] for line in pacman("-Q"))


def install_or_mark_explicit(packages: Iterable[str]):
    config = read_config()
    packages_to_install_or_mark_explicit = (
        set(packages) - get_explicitly_installed_packages()
    )
    packages_to_install = (
        packages_to_install_or_mark_explicit - get_installed_packages()
    )
    packages_to_mark_explicit = (
        packages_to_install_or_mark_explicit & get_installed_packages()
    )
    if packages_to_install:
        run_action(*config.settings.install_command.split(" "), *packages_to_install)
    if packages_to_mark_explicit:
        run_action("sudo", "pacman", "-D", "--asexplicit", *packages_to_mark_explicit)


def mark_as_dependency(packages: Iterable[str]):
    run_action("sudo", "pacman", "-D", "--asdeps", *packages)


def expand_bundles(bundle_names_to_search: Iterable[str], config: Config) -> set[str]:
    bundle_names: set[str] = set()
    bundle_names_to_search = list(bundle_names_to_search)
    while bundle_names_to_search:
        bundle_name = bundle_names_to_search.pop()
        if bundle_name in bundle_names:
            continue
        if bundle_name not in config.bundles:
            print(f"[red]There is no bundle called [bold]{bundle_name}[/bold][/red]")
            raise typer.Exit(1)
        bundle_names.add(bundle_name)
        bundle = config.bundles[bundle_name]
        for member in bundle.members:
            if identifier_type(member) == "bundle":
                bundle_names_to_search.append(member[1:])
    return bundle_names


def get_packages(bundle: Bundle) -> list[str]:
    packages: list[str] = []
    for member in bundle.members:
        match identifier_type(member):
            case "group":
                group_name = member[2:]
                if group_name not in pacman_groups():
                    print(
                        f"[red]There is no bundle called [bold]{group_name}[/bold][/red]"
                    )
                    typer.Exit(1)
                packages.extend(pacman_groups()[group_name])
            case "package":
                packages.append(member)
    return packages


def confirm_action(prompt: str = "Proceed with action?"):
    if state["no_confirm"]:
        return
    typer.confirm(prompt, abort=True)


def get_all_specified_packages():
    config = read_config()
    specified_bundle_names = {
        name for name, bundle in config.bundles.items() if bundle.is_included
    }
    all_bundle_names = expand_bundles(specified_bundle_names, config)
    all_packages = {
        package
        for bundle_name in all_bundle_names
        for package in get_packages(config.bundles[bundle_name])
    }
    return all_packages


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n")] = False,
    no_confirm: Annotated[bool, typer.Option("--no-confirm")] = False,
):
    state["verbose"] = verbose
    state["dry_run"] = dry_run
    state["no_confirm"] = no_confirm


@app.command("list", help="List the bundles in the configuration file.")
def list_packages():
    config = read_config()
    specified_bundle_names = {
        name for name, bundle in config.bundles.items() if bundle.is_included
    }
    all_bundle_names = expand_bundles(specified_bundle_names, config)
    table = Table("Bundle", "Child Bundles", "Packages", "Included")
    for bundle_name, bundle in config.bundles.items():
        child_bundles = (
            member[1:]
            for member in bundle.members
            if identifier_type(member) == "bundle"
        )
        packages_count = Counter(identifier_type(member) for member in bundle.members)
        packages_count_str = f"{packages_count['package']} packages"
        if packages_count["group"]:
            packages_count_str += f"\n{packages_count['group']} groups"
        table.add_row(
            bundle_name,
            ", ".join(child_bundles),
            packages_count_str,
            "✓"
            if bundle.is_included
            else "○"
            if bundle_name in all_bundle_names
            else "✖",
        )
    print("✓: directly included, ○: transitively included, ✖: not included")
    console.print(table)


@app.command(
    "compare",
    help="Compare the difference between of packages in included bundles and those installed in the system.",
)
def compare_packages_difference():
    all_packages = get_all_specified_packages()
    installed_packages = get_explicitly_installed_packages()
    installed_but_not_specified = installed_packages - all_packages
    if installed_but_not_specified:
        print(
            f"There are {len(installed_but_not_specified)} packages are explicitly installed but not specified in included bundles"
        )
        print(Columns(installed_but_not_specified, equal=True, expand=True))
    specified_but_not_installed = all_packages - installed_packages
    if specified_but_not_installed:
        print(
            f"There are {len(specified_but_not_installed)} packages are specified in included bundles but not explicitly installed"
        )
        print(Columns(specified_but_not_installed, equal=True, expand=True))
    if installed_but_not_specified or specified_but_not_installed:
        raise typer.Exit(10)


@app.command("sync", help="Sync system installation with included bundles")
def sync_packages():
    all_packages = get_all_specified_packages()
    installed_packages = get_explicitly_installed_packages()
    installed_but_not_specified = installed_packages - all_packages
    specified_but_not_installed = all_packages - installed_packages

    if installed_but_not_specified:
        print(
            f"The following {len(installed_but_not_specified)} packages will be unmarked as explicitly installed."
        )
        print(Columns(installed_but_not_specified, equal=True, expand=True))
    specified_but_not_installed = all_packages - installed_packages
    if specified_but_not_installed:
        print(
            f"The following {len(specified_but_not_installed)} packages will be installed"
        )
        print(Columns(specified_but_not_installed, equal=True, expand=True))
    if not installed_but_not_specified and not specified_but_not_installed:
        print("Nothing to do")
        raise typer.Exit()
    confirm_action()
    if installed_but_not_specified:
        mark_as_dependency(installed_but_not_specified)
        print(
            "Remember to run [italic]pacman -Rsn $(pacman -Qdtq) to clean up unused dependencies[/italic]"
        )
    if specified_but_not_installed:
        install_or_mark_explicit(specified_but_not_installed)


@app.command("install", help="Install a bundle")
def install_bundle(name: Annotated[str, typer.Argument(help="Name of the bundle")]):
    config = read_config()
    if name not in config.bundles:
        print(f"[red]Bundle {name} does not exist in config.[/red]")
        raise typer.Exit(1)
    all_bundle_names = expand_bundles([name], config)
    all_packages = {
        package
        for bundle_name in all_bundle_names
        for package in get_packages(config.bundles[bundle_name])
    }
    packages_to_install_or_mark_explicit = (
        all_packages - get_explicitly_installed_packages()
    )
    if not packages_to_install_or_mark_explicit:
        print("All packages in the bundle is already installed.")
        raise typer.Exit(0)

    print(
        "[bold]The following packages will be installed or mark as explicitly installed.[/bold]"
    )
    print(Columns(packages_to_install_or_mark_explicit))
    confirm_action()
    install_or_mark_explicit(all_packages)


@app.command("config", help="Edit the config file")
def edit_config():
    if not config_path.is_file():
        print("Creating file")
        try:
            os.makedirs(app_dir, exist_ok=True)
            with open(config_path, "w"):
                pass
        except Exception as e:
            print(f"[red]Failed to create config file:[/red]\n{e}")
            raise typer.Exit(1)
    editor = os.getenv("EDITOR")
    if not editor:
        print("[red]$EDITOR is not set[/red]")
        raise typer.Exit(1)
    subprocess.call([*editor.split(" "), config_path])
