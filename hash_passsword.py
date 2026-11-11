from werkzeug.security import generate_password_hash, check_password_hash

# Enter your super admin password
password = input("Enter Super Admin Password: ")

# Generate secure password hash
hashed_password = generate_password_hash(password, method="scrypt")

print("\n===== PASSWORD HASH =====")
print(hashed_password)

# Verify password
if check_password_hash(hashed_password, password):
    print("\nPassword hash verified successfully!")
