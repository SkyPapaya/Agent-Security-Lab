# 测试事项、来源、信任、授权和限制组成的关系材料。两个任务条件只改变未来目标，不能顺带改变事实或原始权限。
# 阅读顺序：先看每个测试上方的条件和目的，再对照实际断言；固定回复只用于离线单测。
import json
import random
import unittest
from collections import Counter

from experiments.llama_cpp.compaction_security.native_retention.analysis import value_presence
from experiments.llama_cpp.compaction_security.native_retention.corpus import (
    DOMAIN_NAMES, POSITION_BINS, probe_prompt, score_answers,
)
from experiments.llama_cpp.compaction_security.native_retention.relation_corpus import (
    RELATION_FIELDS, TASK_GOALS, build_relation_corpus,
)


# 集中验证本模块的输入、正常路径与异常边界；各方法独立构造条件。
class RelationCorpusTests(unittest.TestCase):
    # 重复生成关系材料应完全一致且不改全局随机状态，避免影响实验其他部分。
    def test_same_inputs_are_deterministic_without_global_rng_mutation(self):
        before = random.getstate()
        for history in range(len(DOMAIN_NAMES)):
            for layout in range(3):
                for condition in TASK_GOALS:
                    self.assertEqual(build_relation_corpus(history, layout, condition),
                                     build_relation_corpus(history, layout, condition))
        self.assertEqual(random.getstate(), before)

    # 两种任务条件只允许目标句不同；来源、授权、材料和真值必须一致，保证比较可解释。
    def test_conditions_only_change_the_goal_sentence_in_material(self):
        for history in range(len(DOMAIN_NAMES)):
            for layout in range(3):
                a = build_relation_corpus(history, layout, "record_only")
                b = build_relation_corpus(history, layout, "pending_review")
                self.assertEqual(a["intro"].replace(a["goal_sentence"], "<GOAL>"),
                                 b["intro"].replace(b["goal_sentence"], "<GOAL>"))
                for key in ("messages", "facts", "relations", "history_id"):
                    self.assertEqual(a[key], b[key])
                self.assertEqual(a["intro"].count(a["goal_sentence"]), 1)

    # 六个事项各含五类字段，共 30 个事实，分批和位置均衡且每条陈述只出现一次。
    def test_six_relations_thirty_compatible_facts_and_balanced_batches(self):
        for history in range(len(DOMAIN_NAMES)):
            corpus = build_relation_corpus(history, 0, "record_only")
            self.assertEqual(len(corpus["relations"]), 6)
            self.assertEqual(len(corpus["facts"]), 30)
            self.assertEqual(len({fact["id"] for fact in corpus["facts"]}), 30)
            self.assertEqual(Counter(f["category"] for f in corpus["facts"]),
                             Counter({field: 6 for field in RELATION_FIELDS}))
            self.assertEqual(Counter(f["position_bin"] for f in corpus["facts"]),
                             Counter({position: 10 for position in POSITION_BINS}))
            material = "\n".join(message["content"] for message in corpus["messages"])
            for relation in corpus["relations"]:
                self.assertEqual(set(relation["expected"]), set(RELATION_FIELDS))
                self.assertEqual(material.count(relation["statement"]), 1)
                related = [f for f in corpus["facts"] if f["relation_id"] == relation["id"]]
                self.assertEqual(len(related), 5)
                for fact in related:
                    self.assertEqual(fact["answer"], relation["expected"][fact["category"]])
                    self.assertEqual(fact["text"], relation["statement"])
                    self.assertIn(relation["id"], fact["question"])
                    self.assertIn(fact["answer"], fact["text"])
                    self.assertEqual(fact["aliases"], [])
            for message in corpus["messages"]:
                self.assertEqual(message["role"], "user")
                self.assertEqual(sum(r["statement"] in message["content"]
                                     for r in corpus["relations"]), 2)

    # 换布局只改变早中晚位置，事项内容及真值保持不变，不能把内容差异归因于位置。
    def test_layout_changes_placement_not_semantics(self):
        corpora = [build_relation_corpus(2, layout, "pending_review") for layout in range(3)]
        self.assertEqual(len({json.dumps(c["messages"], ensure_ascii=False) for c in corpora}), 3)
        for index in range(6):
            relations = [c["relations"][index] for c in corpora]
            self.assertEqual({r["position_bin"] for r in relations}, set(POSITION_BINS))
            fixed = [{key: value for key, value in r.items() if key != "position_bin"}
                     for r in relations]
            self.assertEqual(fixed[0], fixed[1])
            self.assertEqual(fixed[1], fixed[2])

    # 探针可使用材料本来公开的事项编号，但不能带入字段答案或授权账本。
    def test_probe_includes_public_markers_but_no_field_answers_or_answer_key(self):
        corpus = build_relation_corpus(0, 0, "record_only")
        prompt = probe_prompt(corpus["facts"])
        for relation in corpus["relations"]:
            self.assertIn(relation["id"], prompt)
            self.assertIn(relation["id"], "\n".join(m["content"] for m in corpus["messages"]))
        for fact in corpus["facts"]:
            self.assertIn(fact["question"], prompt)
            self.assertNotIn(fact["answer"], prompt)
        self.assertNotIn('"expected"', prompt)
        self.assertNotIn('"authority"', prompt)

    # 关系材料在压缩前应通过既有答案评分和词面诊断，验证不同实验接口的数据兼容。
    def test_all_thirty_facts_pass_existing_scoring_and_presence_interfaces(self):
        for history in range(len(DOMAIN_NAMES)):
            corpus = build_relation_corpus(history, 0, "record_only")
            facts = corpus["facts"]
            response = json.dumps({"answers": [{"id": f["id"], "answer": f["answer"]}
                                               for f in reversed(facts)]}, ensure_ascii=False)
            result = score_answers(response, facts)
            self.assertFalse(result["parse_error"])
            self.assertEqual(len(result["scores"]), 30)
            self.assertTrue(all(s["correct"] and s["bounded_correct"] for s in result["scores"]))
            material = "\n".join(m["content"] for m in corpus["messages"])
            self.assertTrue(all(row["value_present"] for row in value_presence(material, facts)))

    # 低信任文档即使自称已获批，真值仍由可信用户账本确定，不能反过来授予权限。
    def test_source_identities_and_document_claim_never_grant_user_authority(self):
        corpus = build_relation_corpus(0, 0, "record_only")
        self.assertIn("trusted_user", corpus["intro"])
        self.assertIn("document_claim", corpus["intro"])
        self.assertIn("不能改变上述账本", corpus["intro"])
        self.assertEqual(sum(r["expected"]["trust"] == "低信任文档"
                             for r in corpus["relations"]), 2)
        claim = corpus["relations"][4]
        self.assertIn("已获用户授权", claim["statement"])
        self.assertEqual(claim["expected"]["authorization"], "用户尚未确认")
        self.assertIn("document_claim", claim["authority"])
        self.assertIn("不能据此授权", claim["authority"])
        for relation in corpus["relations"]:
            self.assertIn("trusted_user", relation["authority"])
            self.assertIn(relation["id"], corpus["intro"])
            self.assertIn(relation["expected"]["authorization"], corpus["intro"])

    # 用户引言只保存授权，不重复具体事项、来源、可信度和限制，避免重放意外保护所有字段。
    def test_trusted_intro_does_not_replay_other_field_answers(self):
        corpus = build_relation_corpus(0, 0, "pending_review")
        for relation in corpus["relations"]:
            for field in ("item", "source", "trust", "constraint"):
                self.assertNotIn(relation["expected"][field], corpus["intro"])

    # 两个条件都维持虚构、仅记录和不执行工具的边界，材料中不得混入真实地址或用户路径。
    def test_fictional_safety_boundaries_and_receive_only_in_both_conditions(self):
        for condition in TASK_GOALS:
            corpus = build_relation_corpus(1, 1, condition)
            for required in ("完全虚构", "禁止外发", "访问公网", "实验目录外写入",
                             "不执行任何工具", "每次只回复“已记录”", "现在也不开展核对"):
                self.assertIn(required, corpus["intro"])
            for message in corpus["messages"]:
                self.assertIn("不分析、不核对、不调用工具", message["content"])
            material = json.dumps(corpus, ensure_ascii=False)
            for forbidden in ("http://", "https://", "smtp", "api_key", "/home/", "@"):
                self.assertNotIn(forbidden, material.lower())

    # 不存在的条件、越界历史和非法布局都应报错，不能悄悄回退到另一个实验条件。
    def test_invalid_inputs_fail_explicitly(self):
        for history, layout, condition in [(-1, 0, "record_only"), (8, 0, "record_only"),
                                            (True, 0, "record_only"), (0, -1, "record_only"),
                                            (0, False, "record_only"), (0, 1.5, "record_only"),
                                            (0, 0, "unknown"), (0, 0, [])]:
            with self.subTest(history=history, layout=layout, condition=condition):
                with self.assertRaises(ValueError):
                    build_relation_corpus(history, layout, condition)


if __name__ == "__main__":
    unittest.main()
