# Flight Strip Administration & Analysis Pipeline

A robust, production-ready Django application for ingesting, validating, and analyzing aviation flight strip data from Excel files. This project serves as a high-performance data pipeline, leveraging the Polars DataFrame library for efficient data processing and providing a secure interface for data management and auditing.

![Screenshot of the import interface](<path_to_your_screenshot.png>)

## Key Features

- **High-Performance Validation**: Utilizes the blazingly fast Polars library to validate entire Excel files in-memory, checking for data types, required fields, and complex regex patterns.
- **Detailed Error Reporting**: Instead of failing on the first error, the system processes the entire file and returns a detailed, row-by-row breakdown of all validation failures directly in the UI.
- **Secure Admin Interface**: Built on the Django Admin, providing role-based access control, CSRF protection, and other security features out-of-the-box.
- **Transactional Data Import**: Valid data is imported into the database using atomic transactions. The `bulk_create` method with `ignore_conflicts=True` ensures high performance and data integrity.
- **Audit Logging**: Automatically records key user actions (e.g., successful imports, validation failures, critical errors) for security and traceability.
- **Production-Ready**: Includes configurations for security, logging, environment variables, and a Gunicorn web server for deployment.

## Technology Stack

- **Backend**: Django 5.2, Python 3.13
- **Data Processing**: Polars, fastexcel
- **Database**: PostgreSQL (recommended), SQLite (for development)
- **Deployment**: Gunicorn, Decouple (for environment management)
- **Linting/Formatting**: Ruff, djLint

## Setup and Installation

### Prerequisites

- Python 3.13+
- PostgreSQL (recommended) or another Django-compatible database
- A `.env` file for environment variables

### 1. Clone the Repository

```bash
git clone [https://github.com/your-username/flight-strip-administration.git](https://github.com/your-username/flight-strip-administration.git)
cd flight-strip-administration
```

### 2. Set Up Environment Variables

Create a `.env` file in the project root. Use the `.env.example` file as a template.

```ini
# .env
SECRET_KEY='your-strong-secret-key'
DEBUG=True
DATABASE_URL='postgres://user:password@host:port/dbname'
ALLOWED_HOSTS='127.0.0.1,localhost'
```

### 3. Install Dependencies

```bash
python -m venv venv
source venv/bin/activate  # On Windows, use `venv\Scripts\activate`
pip install -r requirements.txt
```

### 4. Apply Database Migrations

```bash
python manage.py migrate
```

### 5. Create a Superuser

```bash
python manage.py createsuperuser
```

## Usage

1. **Run the Development Server**:

    ```bash
    python manage.py runserver
    ```

2. **Access the Admin Panel**:
    Navigate to `http://127.0.0.1:8000/admin` and log in with your superuser credentials.

3. **Grant Import Permissions**:
    - Go to the "Users" section and select your user.
    - Find the "Permissions" section and add the `core | flight strip | Can import strip data` permission.

4. **Import Data**:
    - Navigate to "FSA" > "Flight Strips".
    - Click the "Import Excel" button.
    - Select an Excel file (`.xlsx`) and the correct year for the data, then submit.
    - The system will either report success, show a detailed table of validation errors, or report a critical failure.

## Running Tests

To ensure code quality and prevent regressions, run the test suite:

```bash
python manage.py test core
```

## Security Considerations

This application has been built with security in mind:

- **Environment Variables**: All sensitive keys, database URLs, and environment-specific settings are loaded from a `.env` file and are not hardcoded.
- **Permissions**: The import functionality is protected by a specific Django permission (`core.import_strip`), ensuring only authorized users can upload data.
- **Django Security Middleware**: The project is configured with Django's built-in security features, including CSRF protection, secure headers, and clickjacking defense. See `settings.py` for details.
- **Input Validation**: All uploaded data undergoes rigorous validation before being processed or saved to the database, preventing data corruption and potential injection attacks.

For production, ensure `DEBUG` is set to `False` and configure the security settings in `settings.py` (e.g., `SECURE_SSL_REDIRECT`, `ALLOWED_HOSTS`).

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
