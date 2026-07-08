import psycopg2
host = "aws-0-ap-northeast-1.pooler.supabase.com"
port = "6543"
database = "postgres"
user = "postgres.fqccsacfdtnlnvlhcuec"
passwords = ["Tracnx@12234", "Tracxn@12234"]

for pwd in passwords:
    print(f"Trying {pwd}")
    try:
        connection = psycopg2.connect(host=host, port=port, database=database, user=user, password=pwd)
        print(f"SUCCESS with {pwd}")
        break
    except Exception as e:
        print(f"FAILED with {pwd}: {e}")
