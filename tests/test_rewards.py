from app.rewards import calculate_xp, calculate_stat_rewards, calculate_rewards

def test_calculate_xp_normal_normal():
    assert calculate_xp("normal", "normal") == 40

def test_calculate_xp_normal_demanding():
    assert calculate_xp("normal", "demanding") == 50

def test_calculate_xp_long_normal():
    assert calculate_xp("long", "normal") == 60

def test_calculate_xp_short_easy():
    assert calculate_xp("short", "easy") == 20

def test_calculate_xp_rounds_to_nearest_5():
    # mini (15) * demanding (1.25) = 18.75 → rounds to 20
    assert calculate_xp("mini", "demanding") == 20

def test_calculate_stat_rewards_knowledge_learning():
    stats = calculate_stat_rewards("knowledge_learning", 40)
    assert stats["knowledge"] == round(40 * 70 / 100)
    assert stats["discipline"] == round(40 * 20 / 100)
    assert stats["technique"] == round(40 * 10 / 100)

def test_calculate_rewards_returns_tuple():
    xp, stats = calculate_rewards("normal", "normal", "knowledge_learning")
    assert xp == 40
    assert isinstance(stats, dict)
    assert "knowledge" in stats
