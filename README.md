# Setup

On Windows, use powershell.

CD into this repo.

Install with pip into a virtual environment, in editable mode, as follows:


On Windows

```
 python -m venv venv
  .\venv\Scripts\Activate.ps1
  pip install -e .
```

On Linux/Mac

```
 python3 -m venv venv
 source venv/bin/activate
 pip install -e .
```

You should then be able to run 'tybuild' from the command line.
(Use 'tybuild -h' to get command line help.)

With the virtual environment active, you should also be able to import stuff from 'tybuild' inside other python code.

When finished, the virtual environment can be deactivated with 'deactivate'.

## Requirements

This project is configured using `pyproject.toml` and uses modern Python packaging tools.

- Python 3.9+ ?
- pip ≥ 22.0 (needed for editable installs with `pyproject.toml`)
- setuptools ≥ 61.0
- wheel

If you see an error like:

> File "setup.py" or "setup.cfg" not found. Directory cannot be installed in editable mode  
> (A "pyproject.toml" file was found, but editable mode currently requires a setuptools-based build.)

then your `pip` is too old. Upgrade it inside your virtual environment:

```bash
python -m pip install --upgrade pip
```
