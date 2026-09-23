"""
Generates a bcrypt hash for a new user's password, to paste into config.yaml.
Never store plain-text passwords in config.yaml.

Usage:
    python hash_password.py
"""

import getpass

import streamlit_authenticator as stauth


def main():
    password = getpass.getpass("Password to hash: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords did not match.")
        return
    print("\nPaste this as the user's 'password' value in config.yaml:")
    print(stauth.Hasher().hash(password))


if __name__ == "__main__":
    main()
