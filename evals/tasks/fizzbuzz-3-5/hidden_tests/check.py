import sys
sys.path.insert(0, '.')
from fizz import label
assert label(15) == 'FizzBuzz'
assert label(3) == 'Fizz'
assert label(5) == 'Buzz'
assert label(1) == '1'
print('ok')
