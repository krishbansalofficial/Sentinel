import sys
sys.path.insert(0, '.')
from words import initials
assert initials('ada lovelace') == 'AL'
print('ok')
