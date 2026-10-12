"""Create the Render password hash without echoing or storing the password."""
import getpass
from werkzeug.security import generate_password_hash


if __name__ == "__main__":
    password = getpass.getpass("Shared team password (at least 16 characters): ")
    if len(password) < 16:
        raise SystemExit("Use at least 16 characters.")
    if password != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords do not match.")
    print(generate_password_hash(password, method="pbkdf2:sha256:1000000"))
