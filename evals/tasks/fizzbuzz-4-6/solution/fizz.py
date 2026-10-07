def label(n):
    if n % 4 == 0 and n % 6 == 0:
        return "FizzBuzz"
    if n % 4 == 0:
        return "Fizz"
    if n % 6 == 0:
        return "Buzz"
    return str(n)
