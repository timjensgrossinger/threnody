def validate(record):
    errors = []
    try:
        if 'name' not in record:
            errors.append('missing:name')
        else:
            if not record['name']:
                errors.append('empty:name')
        if 'email' not in record:
            errors.append('missing:email')
        else:
            if not record['email']:
                errors.append('empty:email')
        if 'age' not in record:
            errors.append('missing:age')
        else:
            if record['age'] is None:
                errors.append('empty:age')
    except Exception:
        pass
    return errors
