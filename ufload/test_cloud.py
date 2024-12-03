import ufload

def test_match():
    x = [ 'other', 'OCG_SZ1_NHL-Wed.zip', 'OCG_UG2_SUKA-Fri.zip' ]
    wild = ['SZ1_NHL', 'OCG_UG']
    m = [ufload.cloud._match_any_wildcard(wild, x) for x in x]
    assert(m == [ False, True, True ])

