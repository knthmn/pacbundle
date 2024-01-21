import re
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

app = typer.Typer()
console = Console()

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


class Config(BaseModel):
    bundles: dict[str, Bundle] = Field(default_factory=dict)


def identifier_type(identifier: str):
    if identifier.startswith("#"):
        return "bundle"
    if identifier.startswith("g#"):
        return "group"
    return "package"


def read_config() -> Config:
    config_file = Path(typer.get_app_dir(APP_NAME)) / "config.toml"
    try:
        with open(config_file, "rb") as f:
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
