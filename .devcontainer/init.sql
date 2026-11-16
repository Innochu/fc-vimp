-- Lets the test user create and drop Django's test_vimp_test database.
GRANT ALL PRIVILEGES ON *.* TO 'vimp'@'%';
FLUSH PRIVILEGES;
