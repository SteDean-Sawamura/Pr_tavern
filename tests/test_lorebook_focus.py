import pytest

from engine.lorebook import Lorebook


@pytest.mark.parametrize("subject,topic", [
    ("synthetic_archive_manual", "filing"),
    ("合成资料索引", "存放"),
])
def test_multi_term_excerpt_finds_joint_context_instead_of_isolated_first_mentions(subject, topic):
    filler = "Synthetic unrelated inventory record.\n" * 700
    content = f"{subject}: table of contents.\n{filler}{topic}: historical heading.\n{filler}"
    content += f"{subject} {topic}: SYNTHETIC_RELEVANT_SECTION\nUse the declared storage procedure."
    assert len(content) > 40000
    lorebook = Lorebook([{"id": "test_manual", "keys": [subject], "content": content}])
    result = lorebook.query(keyword=f"{subject} {topic}")
    entry, = result["entries"]
    assert "SYNTHETIC_RELEVANT_SECTION" in entry["content"]
    assert entry["offset"] > 40000
    assert entry["content"] == content[entry["offset"]:entry["offset"] + 400]
    focused = lorebook.query(entry_id=entry["id"], offset=entry["offset"], snapshot=entry["snapshot"])
    assert "SYNTHETIC_RELEVANT_SECTION" in focused["entries"][0]["content"]


def test_repeated_mentions_do_not_outweigh_distinct_query_terms():
    content = "synthetic_archive " * 1000 + "unrelated. " * 100
    content += "synthetic_archive filing: SYNTHETIC_RELEVANT_SECTION"
    lorebook = Lorebook([{"id": "test_manual", "keys": ["synthetic_archive"], "content": content}])
    entry, = lorebook.query(keyword="synthetic_archive filing")["entries"]
    assert "SYNTHETIC_RELEVANT_SECTION" in entry["content"]


@pytest.mark.parametrize("query,date_text", [
    ("合成登记 12月5日", "十二月五日"),
    ("合成登记 十二月五号", "12月5日"),
])
def test_joint_excerpt_preserves_date_aliases_and_original_offsets(query, date_text):
    content = "合成登记：目录。" + "无关填充。" * 1000 + date_text + "：旧目录。"
    content += "无关填充。" * 1000 + f"合成登记 {date_text}：SYNTHETIC_RELEVANT_SECTION"
    lorebook = Lorebook([{"id": "test_manual", "keys": ["合成登记"], "content": content}])
    entry, = lorebook.query(keyword=query)["entries"]
    assert "SYNTHETIC_RELEVANT_SECTION" in entry["content"]
    assert entry["content"] == content[entry["offset"]:entry["offset"] + 400]


def test_casefold_expansion_preserves_source_coordinates():
    content = "ß" * 1000 + "Synthetic_Archive filing: SYNTHETIC_RELEVANT_SECTION"
    lorebook = Lorebook([{"id": "test_manual", "keys": ["synthetic_archive"], "content": content}])
    entry, = lorebook.query(keyword="synthetic_archive filing")["entries"]
    assert entry["offset"] == 920
    assert "SYNTHETIC_RELEVANT_SECTION" in entry["content"]
    assert entry["content"] == content[920:1320]


def test_clipped_term_does_not_count_as_joint_context():
    subject = "synthetic_archive"
    content = "." * 1000 + subject + "." * (317 - len(subject)) + "filing"
    content += "." * 1000 + "synthetic_archive filing: SYNTHETIC_RELEVANT_SECTION"
    lorebook = Lorebook([{"id": "test_manual", "keys": [subject], "content": content}])
    entry, = lorebook.query(keyword="synthetic_archive filing")["entries"]
    assert "SYNTHETIC_RELEVANT_SECTION" in entry["content"]


def test_date_spellings_count_as_one_term():
    content = "12月5日 十二月五号 " * 100 + "." * 1000
    content += "合成登记 十二月五日：SYNTHETIC_RELEVANT_SECTION"
    lorebook = Lorebook([{"id": "test_manual", "keys": ["合成登记"], "content": content}])
    entry, = lorebook.query(keyword="合成登记 12月5日")["entries"]
    assert "SYNTHETIC_RELEVANT_SECTION" in entry["content"]


@pytest.mark.parametrize("keyword,expected_position", [
    ("filing", 500),
    ("filing synthetic_archive", 2000),
    ("missing_body_term", 0),
])
def test_equal_coverage_prefers_longer_then_earlier_match(keyword, expected_position):
    content = "." * 500 + "filing" + "." * 1494 + "synthetic_archive" + "." * 1000 + "synthetic_archive"
    lorebook = Lorebook([{"id": "test_manual", "keys": ["missing_body_term"], "content": content}])
    entry, = lorebook.query(keyword=keyword)["entries"]
    assert entry["offset"] == max(0, expected_position - 80)
