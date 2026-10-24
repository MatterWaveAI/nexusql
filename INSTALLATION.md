# NexusQL Installation Guide

## Quick Start

### For Development (Editable Install)

```bash
# From the nexusql directory
pip install -e .

# Or with all database drivers
pip install -e ".[all]"

# Or with development dependencies
pip install -e ".[dev]"
```

### For Production

```bash
# Install from PyPI (once published)
pip install nexusql

# With specific database support
pip install nexusql[postgresql]  # PostgreSQL only
pip install nexusql[mysql]        # MySQL only
pip install nexusql[mssql]        # MSSQL only
pip install nexusql[all]          # All databases
```

## Development Install

```bash
git clone https://github.com/matterwaveai/nexusql
cd nexusql
pip install -e ".[dev]"
```

## Verifying Installation

```python
import nexusql
print(f"NexusQL version: {nexusql.__version__}")

from nexusql import DatabaseManager
db = DatabaseManager("sqlite:///:memory:")
db.connect()
print("✓ NexusQL is working!")
db.disconnect()
```

## Package Structure

```
nexusql/
├── nexusql/                  # Main package
│   ├── __init__.py          # Package exports
│   ├── interfaces.py        # Database interfaces and types
│   ├── manager.py           # DatabaseManager implementation
│   ├── migrations.py        # Migration system
│   └── migrations/          # SQL migration files
│       ├── V001__complete_schema.sql
│       └── V002__hitl_schema.sql
├── tests/                   # Test suite
│   ├── unit/               # Unit tests
│   └── integration/        # Integration tests
├── docs/                    # Documentation
├── pyproject.toml          # Package configuration
├── README.md               # Main documentation
└── LICENSE                 # MIT License
```

## Development Workflow

### 1. Make Changes
Edit files in `nexusql/nexusql/`

### 2. Test Changes
```bash
# Run tests
pytest

# Run with coverage
pytest --cov=nexusql --cov-report=html

# Test specific database
pytest tests/integration/test_database_mysql.py -v
```

### 3. Build Distribution
```bash
# Install build tools
pip install build twine

# Build package
python -m build

# This creates:
# - dist/nexusql-0.1.0-py3-none-any.whl
# - dist/nexusql-0.1.0.tar.gz
```

### 4. Publish (When Ready)
```bash
# Test on TestPyPI first
python -m twine upload --repository testpypi dist/*

# Publish to PyPI
python -m twine upload dist/*
```

## Troubleshooting

### Import Errors

If you get `ImportError: cannot import name 'DatabaseManager' from 'nexusql'`:

1. Make sure nexusql is installed: `pip list | grep nexusql`
2. Reinstall: `pip uninstall nexusql && pip install -e .`
3. Check Python path: `python -c "import nexusql; print(nexusql.__file__)"`

### Migration Files Not Found

Make sure migration files are included in the package:

1. Check MANIFEST.in includes migrations
2. Verify pyproject.toml has package-data configuration
3. Reinstall: `pip install -e .`

### Database Connection Issues

For PostgreSQL, MySQL, or MSSQL, ensure the drivers are installed:

```bash
# PostgreSQL (psycopg 3)
pip install "psycopg[binary,pool]"

# MySQL
pip install pymysql

# MSSQL
pip install pyodbc
```

## Next Steps

- Read the [README.md](README.md) for usage examples
- Check [tests/](tests/) for comprehensive examples
- Explore [nexusql/](nexusql/) source code for advanced features
