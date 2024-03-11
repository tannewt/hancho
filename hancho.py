#!/usr/bin/python3

"""Hancho is a simple, pleasant build system."""

import argparse
import asyncio
import builtins
import inspect
import io
import json
import os
import re
import subprocess
import sys
import traceback
import types
from pathlib import Path
from os.path import relpath
from os.path import abspath
from glob import glob

# If we were launched directly, a reference to this module is already in
# sys.modules[__name__]. Stash another reference in sys.modules["hancho"] so
# that build.hancho and descendants don't try to load a second copy of Hancho.

this = sys.modules[__name__]
sys.modules["hancho"] = sys.modules[__name__]

config = None

################################################################################
# Build rule helper methods


def color(red=None, green=None, blue=None):
    """Converts RGB color to ANSI format string"""
    # Color strings don't work in Windows console, so don't emit them.
    if os.name == "nt":
        return ""
    if red is None:
        return "\x1B[0m"
    return f"\x1B[38;2;{red};{green};{blue}m"


def is_atom(element):
    """Returns True if 'element' should _not_ be flattened out"""
    return isinstance(element, str) or not hasattr(element, "__iter__")


def run_cmd(cmd):
    """Runs a console command and returns its stdout with whitespace stripped"""
    return subprocess.check_output(cmd, shell=True, text=True).strip()


def swap_ext(name, new_ext):
    """
    Replaces file extensions on either a single filename or a list of filenames
    """
    if name is None:
        return None
    if is_atom(name):
        return Path(name).with_suffix(new_ext)
    return [swap_ext(n, new_ext) for n in flatten(name)]


def mtime(filename):
    """Gets the file's mtime and tracks how many times we called it"""
    this.mtime_calls += 1
    return Path(filename).stat().st_mtime


def flatten(elements):
    """
    Converts an arbitrarily-nested list 'elements' into a flat list, or wraps it
    in [] if it's not a list.
    """
    if is_atom(elements):
        return [elements]
    result = []
    for element in elements:
        result.extend(flatten(element))
    return result


def maybe_as_number(text):
    """
    Tries to convert a string to an int, then a float, then gives up. Used for
    ingesting unrecognized flag values.
    """
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


class Chdir():
    """Copied from Python 3.11 contextlib.py"""
    def __init__(self, path):
        self.path = path
        self._old_cwd = []

    def __enter__(self):
        self._old_cwd.append(os.getcwd())
        os.chdir(self.path)

    def __exit__(self, *excinfo):
        os.chdir(self._old_cwd.pop())


################################################################################

this.line_dirty = False


def log(message, *args, sameline=False, **kwargs):
    """Simple logger that can do same-line log messages like Ninja"""
    if config.quiet:
        return

    if not sys.stdout.isatty():
        sameline = False

    output = io.StringIO()
    if sameline:
        kwargs["end"] = ""
    print(message, *args, file=output, **kwargs)
    output = output.getvalue()

    if not sameline and this.line_dirty:
        sys.stdout.write("\n")
        this.line_dirty = False

    if not output:
        return

    if sameline:
        sys.stdout.write("\r")
        output = output[: os.get_terminal_size().columns - 1]
        sys.stdout.write(output)
        sys.stdout.write("\x1B[K")
    else:
        sys.stdout.write(output)

    sys.stdout.flush()
    this.line_dirty = output[-1] != "\n"


################################################################################


def main():
    """
    Our main() just handles command line args and delegates to async_main()
    """

    # pylint: disable=line-too-long
    # fmt: off
    parser = argparse.ArgumentParser()
    parser.add_argument("filename",        default="build.hancho", type=str, nargs="?", help="The name of the .hancho file to build")
    parser.add_argument("-C", "--chdir",   default=".",            type=str,            help="Change directory first")
    parser.add_argument("-j", "--jobs",    default=os.cpu_count(), type=int,            help="Run N jobs in parallel (default = cpu_count, 0 = infinity)")
    parser.add_argument("-v", "--verbose", default=False,          action="store_true", help="Print verbose build info")
    parser.add_argument("-q", "--quiet",   default=False,          action="store_true", help="Mute all output")
    parser.add_argument("-n", "--dryrun",  default=False,          action="store_true", help="Do not run commands")
    parser.add_argument("-d", "--debug",   default=False,          action="store_true", help="Print debugging information")
    parser.add_argument("-f", "--force",   default=False,          action="store_true", help="Force rebuild of everything")
    # fmt: on

    (flags, unrecognized) = parser.parse_known_args()

    if not flags.jobs:
        flags.jobs = 1000

    # We set this to None first so that config.base gets sets to None in the
    # next line.
    global config
    config = None

    config = Rule(
        filename="build.hancho",
        chdir=".",
        jobs=os.cpu_count(),
        verbose=False,
        quiet=False,
        dryrun=False,
        debug=False,
        force=False,
        desc="{files_in} -> {files_out}",
        build_dir="build",
        task_dir=".",
        files_out=[],
        deps=[],
        len=len,
        run_cmd=run_cmd,
        swap_ext=swap_ext,
        color=color,
        glob=glob,
    )

    config |= flags.__dict__

    config.filename = Path(config.filename).absolute()

    config.hancho_root = Path.cwd()

    # Unrecognized flags become global config fields.
    for span in unrecognized:
        if match := re.match(r"-+([^=\s]+)(?:=(\S+))?", span):
            config[match.group(1)] = (
                maybe_as_number(match.group(2)) if match.group(2) is not None else True
            )

    # Configure our job semaphore
    config.semaphore = asyncio.Semaphore(flags.jobs)

    with Chdir(config.chdir):
        return asyncio.run(async_main())


################################################################################


async def async_main():
    """All the actual Hancho stuff runs in an async context."""

    # Reset all global state
    this.hancho_root = Path.cwd()
    this.hancho_mods = {}
    this.mod_stack = []
    this.hancho_outs = set()
    this.tasks_total = 0
    this.tasks_pass = 0
    this.tasks_fail = 0
    this.tasks_skip = 0
    this.task_counter = 0
    this.mtime_calls = 0

    # Load top module(s).
    if not config.filename.exists():
        raise FileNotFoundError(f"Could not find {config.filename}")

    root_filename = config.filename.resolve()
    load_abs(root_filename)

    # Top module(s) loaded. Run all tasks in the queue until we run out.

    while True:
        pending_tasks = asyncio.all_tasks() - {asyncio.current_task()}
        if not pending_tasks:
            break
        await asyncio.wait(pending_tasks)

    # Done, print status info if needed
    if config.debug or config.verbose:
        log(f"tasks total:   {this.tasks_total}")
        log(f"tasks passed:  {this.tasks_pass}")
        log(f"tasks failed:  {this.tasks_fail}")
        log(f"tasks skipped: {this.tasks_skip}")
        log(f"mtime calls:   {this.mtime_calls}")

    if this.tasks_fail:
        log(f"hancho: {color(255, 0, 0)}BUILD FAILED{color()}")
    elif this.tasks_pass:
        log(f"hancho: {color(0, 255, 0)}BUILD PASSED{color()}")
    else:
        log(f"hancho: {color(255, 255, 0)}BUILD CLEAN{color()}")

    return -1 if this.tasks_fail else 0


################################################################################
# The .hancho file loader does a small amount of work to keep track of the
# stack of .hancho files that have been loaded.


def load(mod_path):
    """
    Searches the loaded Hancho module stack for a module whose directory
    contains 'mod_path', then loads the module relative to that path.
    """
    mod_path = Path(mod_path)
    for parent_mod in reversed(this.mod_stack):
        abs_path = (Path(parent_mod.__file__).parent / mod_path).resolve()
        if abs_path.exists():
            return load_abs(abs_path)
    raise FileNotFoundError(f"Could not load module {mod_path}")


def load_abs(abs_path):
    """
    Loads a Hancho module ***while chdir'd into its directory***
    """
    abs_path = Path(abs_path)
    if abs_path in this.hancho_mods:
        return this.hancho_mods[abs_path]

    with open(abs_path, encoding="utf-8") as file:
        source = file.read()
        code = compile(source, abs_path, "exec", dont_inherit=True)

    module = type(sys)(abs_path.stem)
    module.__file__ = abs_path
    module.__builtins__ = builtins
    this.hancho_mods[abs_path] = module

    sys.path.insert(0, str(abs_path.parent))

    # We must chdir()s into the .hancho file directory before running it so that
    # glob() can resolve files relative to the .hancho file itself.
    this.mod_stack.append(module)

    with Chdir(abs_path.parent):
        # Why Pylint thinks this is not callable is a mystery.
        # pylint: disable=not-callable
        types.FunctionType(code, module.__dict__)()

    this.mod_stack.pop()

    return module


################################################################################
# expand + await + flatten

template_regex = re.compile("{[^}]*}")


async def expand_async(rule, template, depth=0):
    """
    A trivial templating system that replaces {foo} with the value of rule.foo
    and keeps going until it can't replace anything. Templates that evaluate to
    None are replaced with the empty string.
    """

    if depth == 10:
        raise ValueError(f"Expanding '{str(template)[0:20]}...' failed to terminate")

    # Awaitables get awaited
    if inspect.isawaitable(template):
        template = await template

    # Cancellations cancel this task
    if isinstance(template, Cancel):
        raise template

    # Functions just get passed through
    # if inspect.isfunction(template):
    #    return template
    assert not inspect.isfunction(template)

    # Nones become empty strings
    if template is None:
        return ""

    # Lists get flattened and joined
    if isinstance(template, list):
        template = await flatten_async(rule, template, depth + 1)
        return " ".join(template)

    # Non-strings get stringified
    if not isinstance(template, str):
        template = str(template)

    # Templates get expanded
    result = ""
    while span := template_regex.search(template):
        result += template[0 : span.start()]
        exp = template[span.start() : span.end()]
        try:
            print("replace", exp[1:-1], rule.input_files)
            replacement = eval(exp[1:-1], globals(), rule)  # pylint: disable=eval-used
            replacement = await expand_async(rule, replacement, depth + 1)
            result += replacement
        except Exception:  # pylint: disable=broad-except
            result += exp
        template = template[span.end() :]
    result += template

    return result


async def flatten_async(rule, elements, depth=0):
    """
    Similar to expand_async, this turns an arbitrarily-nested array of template
    strings and promises into a flat array of plain strings.
    """

    if not isinstance(elements, list):
        elements = [elements]

    result = []
    for element in elements:
        if inspect.isfunction(element):
            result.append(element)
        elif isinstance(element, list):
            new_element = await flatten_async(rule, element, depth + 1)
            result.extend(new_element)
        else:
            new_element = await expand_async(rule, element, depth + 1)
            result.append(new_element)

    return result


################################################################################


class Cancel(BaseException):
    """
    Stub exception class that's used to cancel tasks that depend on a task that
    threw a real exception.
    """


################################################################################
# We have to disable 'attribute-defined-outside-init' because of the attribute
# inheritance we're implementing through '__missing__' - if we define
# everything in __init__, __missing__ won't fire and we won't see the base
# instance's version of that attribute.
# pylint: disable=attribute-defined-outside-init


class Rule(dict):
    """
    Hancho's Rule object behaves like a Javascript object and implements a basic
    form of prototypal inheritance via Rule.base
    """

    # pylint: disable=too-many-instance-attributes
    def __init__(self, *, base=None, **kwargs):
        super().__init__(self)
        self |= kwargs
        self.base = config if base is None else base
        if base is None:
            print([(x.filename, x.function) for x in inspect.stack(context=0)])
            self.task_dir = Path(inspect.stack(context=0)[1].filename).parent
            print("captured task dir", self.task_dir)
        else:
            self.task_dir = base.task_dir

    def __missing__(self, key):
        if self.base:
            return self.base[key]
        return None

    def __setattr__(self, key, value):
        self.__setitem__(key, value)

    def __getattr__(self, key):
        return self.__getitem__(key)

    def __repr__(self):
        """Turns this rule into a JSON doc for debugging"""

        class Encoder(json.JSONEncoder):
            """Turns functions and tasks into stub strings for dumping."""

            def default(self, o):
                if callable(o):
                    return "<function>"
                if isinstance(o, asyncio.Task):
                    return "<task>"
                if isinstance(o, Path):
                    return f"Path {o}"
                if isinstance(o, asyncio.Semaphore):
                    return f"Semaphore"
                return super().default(o)

        return json.dumps(self, indent=2, cls=Encoder)

    def extend(self, **kwargs):
        """
        Returns a 'subclass' of this Rule that can override this rule's fields.
        """
        return Rule(base=self, **kwargs)

    def __call__(self, files_in, files_out=None, **kwargs):
        this.tasks_total += 1
        task = self.extend()
        task.files_in = files_in
        if files_out is not None:
            task.files_out = files_out
        task
        task.file_root = Path(inspect.stack(context=0)[1].filename).parent
        print("capturing script directory", task.file_root)
        task |= kwargs
        coroutine = task.async_call()
        task.promise = asyncio.create_task(coroutine)
        return task.promise

    ########################################

    async def async_call(self):
        """Entry point for async task stuff."""
        try:
            result = await self.dispatch()
            return result

        # If any of this tasks's dependencies were cancelled, we propagate the
        # cancellation to downstream tasks.
        except Cancel as cancel:
            this.tasks_skip += 1
            return cancel

        # If this task failed, we print the error and propagate a Cancel
        # exception to downstream tasks.
        except Exception:  # pylint: disable=broad-except
            if not self.quiet:
                log(color(255, 128, 128))
                traceback.print_exception(*sys.exc_info())
                log(color())
            this.tasks_fail += 1
            return Cancel()

    ########################################
    # pylint: disable=too-many-return-statements,too-many-branches

    async def dispatch(self):
        """Does all the bookkeeping and dependency checking, then runs the command if needed."""
        desc = await expand_async(self, self.desc)

        # Check for missing fields
        if not self.command:
            raise ValueError(f"Command missing for input {self.files_in}!")
        if self.files_in is None:
            raise ValueError(f"Task {desc} missing files_in")
        if self.files_out is None:
            raise ValueError(f"Task {desc} missing files_out")

        # Flatten+await all filename promises in any of the input filename arrays.

        self.files_in = await flatten_async(self, self.files_in)
        self.files_out = await flatten_async(self, self.files_out)
        self.deps = await flatten_async(self, self.deps)

        # Prepend directories to filenames and then normalize + absolute them.
        # If they're already absolute, this does nothing.

        self.build_dir2 = Path(await expand_async(self, self.build_dir))
        self.build_dir2 = (
            this.hancho_root
            / self.build_dir2
        )
        print(self.build_dir2)
        self.src_dir = this.hancho_root / self.file_root.relative_to(this.hancho_root)

        # FIXME - We need to do this with self.task_dir as well

        self.abs_files_in = [self.src_dir / f for f in self.files_in]
        print(self.files_out)
        self.abs_files_out = [self.build_dir2 / f for f in self.files_out]
        self.abs_deps = [self.src_dir / f for f in self.deps]

        # Strip hancho_root off the absolute paths to produce root-relative paths
        self.files_in = [f.relative_to(this.hancho_root) for f in self.abs_files_in]
        print(self.files_in)
        self.files_out = [f.relative_to(this.hancho_root) for f in self.abs_files_out]
        print(self.files_out)
        self.deps = [f.relative_to(this.hancho_root) for f in self.abs_deps]

        # Check for duplicate task outputs
        for file in self.abs_files_out:
            res_file = file.resolve()
            if res_file in this.hancho_outs:
                rel_file = relpath(res_file, config.hancho_root)
                raise NameError(f"Multiple rules build {rel_file}!")
            this.hancho_outs.add(res_file)

        # Check if we need a rebuild
        self.reason = await self.needs_rerun()
        if not self.reason:
            this.tasks_skip += 1
            return self.abs_files_out

        # Make sure our output directories exist
        if not self.dryrun:
            for file_out in self.abs_files_out:
                file_out.parent.mkdir(parents=True, exist_ok=True)

        # And flatten+expand our command list
        commands = await flatten_async(self, self.command)

        # OK, we're ready to start the task. Grab the semaphore before we start
        # printing status stuff so that it'll end up near the actual task
        # invocation.
        async with self.semaphore:

            # Deps fulfilled, we are now runnable so grab a task index.
            this.task_counter += 1
            self.task_index = this.task_counter

            # Print the "[1/N] Foo foo.foo foo.o" status line and debug information
            log(
                f"[{self.task_index}/{this.tasks_total}] {desc}",
                sameline=not self.verbose,
            )

            if self.verbose or self.debug:
                log(f"Reason: {self.reason}")
                for command in commands:
                    log(f">>>{' (DRY RUN)' if self.dryrun else ''} {command}")
                if self.debug:
                    log(self)

            result = []
            for command in commands:
                result = await self.run_command(command)

        # Task complete, check if it actually updated all the output files
        if self.files_in and self.files_out and not self.dryrun:
            if second_reason := await self.needs_rerun():
                raise ValueError(
                    f"Task '{desc}' still needs rerun after running!\n"
                    + f"Reason: {second_reason}"
                )

        this.tasks_pass += 1
        return result

    ########################################
    # Note - We should _not_ be expanding any templates in this step, that
    # should've been done already.

    async def run_command(self, command):
        """Actually runs a command, either by calling it or running it in a subprocess"""

        # Early exit if this is just a dry run
        if self.dryrun:
            return self.abs_files_out

        # Custom commands just get await'ed and then early-out'ed.
        if callable(command):
            with Chdir(self.task_dir):
                result = command(self)
            if inspect.isawaitable(result):
                result = await result
            if result is None:
                raise ValueError(f"Command {command} returned None")
            return result

        # Non-string non-callable commands are not valid
        if not isinstance(command, str):
            raise ValueError(f"Don't know what to do with {command}")

        # Create the subprocess via asyncio and then await the result.
        with Chdir(self.task_dir):
            print("changing to", self.task_dir)
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        (stdout_data, stderr_data) = await proc.communicate()

        self.stdout = stdout_data.decode()
        self.stderr = stderr_data.decode()
        self.returncode = proc.returncode

        # Print command output if needed
        if not self.quiet and (self.stdout or self.stderr):
            if self.stderr:
                log(self.stderr, end="")
            if self.stdout:
                log(self.stdout, end="")

        # Task complete, check the task return code
        if self.returncode:
            raise ValueError(
                f"Command '{command}' exited with return code {self.returncode}"
            )

        # Task passed, return the output file list
        return self.files_out

    ########################################
    # Pylint really doesn't like this function, lol.
    # pylint: disable=too-many-return-statements,too-many-branches

    async def needs_rerun(self):
        """Checks if a task needs to be re-run, and returns a non-empty reason if so."""
        files_in = self.abs_files_in
        files_out = self.abs_files_out

        if self.force:
            return f"Files {self.files_out} forced to rebuild"
        if not files_in:
            return "Always rebuild a target with no inputs"
        if not files_out:
            return "Always rebuild a target with no outputs"

        # Tasks with missing outputs always run.
        for file_out in files_out:
            if not file_out.exists():
                return f"Rebuilding {self.files_out} because some are missing"

        min_out = min(mtime(f) for f in files_out)

        # Check the hancho file(s) that generated the task
        if max(mtime(f) for f in this.hancho_mods.keys()) >= min_out:
            return f"Rebuilding {self.files_out} because its .hancho files have changed"

        # Check user-specified deps.
        if self.deps and max(mtime(f) for f in self.deps) >= min_out:
            return (
                f"Rebuilding {self.files_out} because a manual dependency has changed"
            )

        # Check GCC-format depfile, if present.
        if self.depfile:
            depfile = Path(await expand_async(self, self.depfile))
            abs_depfile = (this.hancho_root / depfile).absolute()
            if abs_depfile.exists():
                if self.debug:
                    log(f"Found depfile {abs_depfile}")
                with open(abs_depfile, encoding="utf-8") as depfile:
                    deplines = None
                    if os.name == "nt":
                        # MSVC /sourceDependencies json depfile
                        deplines = json.load(depfile)["Data"]["Includes"]
                    elif os.name == "posix":
                        # GCC .d depfile
                        deplines = depfile.read().split()
                        deplines = [d for d in deplines[1:] if d != "\\"]
                    if deplines and max(mtime(f) for f in deplines) >= min_out:
                        return (
                            f"Rebuilding {self.files_out} because a dependency in "
                            + f"{abs_depfile} has changed"
                        )

        # Check input files.
        if files_in and max(mtime(f) for f in files_in) >= min_out:
            return f"Rebuilding {self.files_out} because an input has changed"

        # All checks passed, so we don't need to rebuild this output.
        if self.debug:
            log(f"Files {self.files_out} are up to date")

        # All deps were up-to-date, nothing to do.
        return None


################################################################################

if __name__ == "__main__":
    sys.exit(main())
