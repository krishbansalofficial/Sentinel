def label(n):
    if n % 5 == 0 and n % 9 == 0:
        return "FizzBuzz"
    if n % 5 == 0:
        return "Fizz"
    if n % 9 == 0:
        return "Buzz"
    return str(n)
