import sqlite3, os
db = sqlite3.connect(os.path.join('instance', 'smarthr.db'))
db.row_factory = sqlite3.Row
c = db.cursor()
r = c.execute("SELECT e.email, r.role_name, r.role_id FROM Employee e JOIN Role r ON e.role_id = r.role_id WHERE e.email='hr@smarthr.my'").fetchone()
print('hr@smarthr.my ->', dict(r))
print()
print('All roles:')
for r in c.execute('SELECT * FROM Role').fetchall():
    print(' ', dict(r))
