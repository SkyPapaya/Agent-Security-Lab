# 测试 Codex 配置读取、凭据隔离和离线实验流程。配置与凭据使用临时虚构文件；会话和模型响应均由本地替身提供。
# 阅读顺序：先看每个测试上方的条件和目的，再对照实际断言；固定回复只用于离线单测。
import copy
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from experiments.llama_cpp.compaction_security.native_retention.codex_backend import (
    CodexError, context_view, isolated_environment, load_provider, private_config)
from experiments.llama_cpp.compaction_security.native_retention.codex_runner import (
    Experiment, exit_status, input_message, probe_request, role_items)
from experiments.llama_cpp.compaction_security.native_retention.corpus import build_corpus


# 集中验证本模块的输入、正常路径与异常边界；各方法独立构造条件。
class CodexBackendTests(unittest.TestCase):
    # 在临时目录生成虚构供应商配置和 OAuth 文件，测试只读取这些夹具，不接触日常凭据。
    def fixture_provider(self, root):
        config = root / 'config.toml'
        config.write_text('model="fiction-model"\nmodel_provider="sub2api"\n'
                          '[model_providers.sub2api]\nbase_url="https://example.invalid/v1"\n'
                          'wire_api="responses"\nenv_key="SUB2API_API_KEY"\n')
        auth = root / 'auth.json'
        auth.write_text(json.dumps({'auth_mode': 'chatgpt', 'OPENAI_API_KEY': None,
                                   'tokens': {'access_token': 'oauth-only-not-provider'}}))
        return config, auth

    # 只提供日常 OAuth token 时必须报错；只有显式供应商凭据才可用于第三方线路。
    def test_oauth_never_used_as_third_party_key(self):
        with tempfile.TemporaryDirectory() as temp:
            config, auth = self.fixture_provider(Path(temp))
            with self.assertRaises(CodexError):
                load_provider(config, auth, {})
            p = load_provider(config, auth, {'SUB2API_API_KEY': 'fiction-provider-key'})
            self.assertEqual(p['api_key'], 'fiction-provider-key')
            self.assertEqual(p['credential_source'], 'provider_env:SUB2API_API_KEY')

    # 同时提供文件和环境变量两种虚构 key，确认按预定优先级选择而不混用。
    def test_explicit_api_key_file_takes_precedence(self):
        with tempfile.TemporaryDirectory() as temp:
            config, auth = self.fixture_provider(Path(temp))
            auth.write_text('{"OPENAI_API_KEY":"fiction-file-key"}')
            p = load_provider(config, auth, {'SUB2API_API_KEY': 'fiction-other'})
            self.assertEqual(p['api_key'], 'fiction-file-key')
            self.assertEqual(p['model'], 'fiction-model')

    # 运行参数可临时切换模型、强度和服务档位，但原配置文件内容必须保持不变。
    def test_run_overrides_do_not_change_config_values(self):
        with tempfile.TemporaryDirectory() as temp:
            config, auth = self.fixture_provider(Path(temp))
            p = load_provider(config, auth, {'SUB2API_API_KEY': 'fiction-key'},
                              model_override='alternate-model',
                              reasoning_effort_override='low',
                              service_tier_override='priority')
            self.assertEqual((p['model'], p['reasoning_effort'], p['service_tier']),
                             ('alternate-model', 'low', 'priority'))
            self.assertIn('fiction-model', config.read_text())

    # 非法强度在构造运行配置时立即失败，不等到真实请求时才由供应商报错。
    def test_invalid_run_reasoning_effort_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            config, auth = self.fixture_provider(Path(temp))
            with self.assertRaises(CodexError):
                load_provider(config, auth, {'SUB2API_API_KEY': 'fiction-key'},
                              reasoning_effort_override='invalid')

    # 构造带会话标识、代理和密钥的父环境，确认实验子进程移除敏感继承且不改父环境。
    def test_parent_identity_and_credentials_do_not_enter_child(self):
        parent = {'PATH': '/bin', 'HOME': '/normal', 'CODEX_HOME': '/normal/state',
                  'CODEX_THREAD_ID': 'normal-thread', 'CODEX_CI': '1', 'HTTPS_PROXY': 'normal-proxy',
                  'SUB2API_API_KEY': 'fiction-secret', 'OPENAI_API_KEY': 'fiction-secret2'}
        with patch.dict(os.environ, parent, clear=True):
            child = isolated_environment()
            self.assertEqual(dict(os.environ), parent)
        self.assertEqual(child['HOME'], parent['HOME'])
        self.assertEqual(child['CODEX_HOME'], parent['CODEX_HOME'])
        self.assertNotIn('CODEX_THREAD_ID', child)
        self.assertNotIn('HTTPS_PROXY', child)
        self.assertNotIn('OPENAI_API_KEY', child)
        self.assertEqual(child['SUB2API_API_KEY'], 'experiment-loopback-placeholder')

    # 生成配置必须能被 TOML 解析，关闭宿主工具、指向回环网关，并且不嵌入供应商密钥。
    def test_private_config_is_parseable_and_secret_free(self):
        p = {'model': 'fiction-model', 'reasoning_effort': 'high', 'service_tier': 'default',
             'api_key': 'fiction-secret'}
        text = private_config(p, 'http://127.0.0.1:1234/v1', 24000, Path('/experiment'))
        config = tomllib.loads(text)
        self.assertFalse(config['features']['shell_tool'])
        self.assertEqual(config['model_auto_compact_token_limit'], 24000)
        self.assertEqual(config['model_providers']['sub2api']['base_url'], 'http://127.0.0.1:1234/v1')
        self.assertNotIn('fiction-secret', text)

    # 可读工具输出和推理摘要应进入观测文本；加密状态另行标记，不能由可读投影推断全部记忆。
    def test_view_keeps_tool_outputs_and_readable_reasoning_summary(self):
        p = {'input': [{'type': 'reasoning', 'encrypted_content': 'opaque',
                        'summary': [{'type': 'summary_text', 'text': 'fiction-code-41'}]},
                       {'type': 'function_call_output', 'output': 'fiction-source-52'}]}
        v = context_view(p)
        self.assertIn('fiction-code-41', v['text'])
        self.assertIn('fiction-source-52', v['text'])
        self.assertTrue(v['opaque_reasoning'])
        self.assertFalse(v['opaque'])  # only readable-text observations, never a full-state claim
        p['input'].append({'type': 'compaction', 'encrypted_content': 'opaque-compact'})
        self.assertTrue(context_view(p)['opaque'])

    # 探针复制历史后追加问题，既不能改原历史，也不能把答案或工具放进探针。
    def test_probe_is_detached_and_contains_no_answer_key(self):
        facts = build_corpus(0, 0)['facts']
        initial = [input_message('user', 'fiction-history')]
        saved = copy.deepcopy(initial)
        p = probe_request({}, initial, facts, 'fiction-model', 'high')
        self.assertEqual(initial, saved)
        self.assertEqual(p['tools'], [])
        self.assertEqual(len(p['input']), 2)
        self.assertTrue(all(f['answer'] not in p['input'][-1]['content'][0]['text'] for f in facts))

    # 检查人工材料确实编码为相应角色；function_call/output 是固定角色夹具，不是模型实际调用。
    def test_role_diagnostics_use_real_response_roles_without_tool_execution(self):
        messages = [{'content': 'fictional record'}]
        self.assertEqual(role_items('assistant', messages, 'cell')[0]['role'], 'assistant')
        tool = role_items('tool', messages, 'cell')
        self.assertEqual([x['type'] for x in tool], ['function_call', 'function_call_output'])
        self.assertEqual(tool[0]['name'], 'read_synthetic_fixture')
        self.assertEqual(tool[1]['output'], 'fictional record')


# 集中验证本模块的离线执行流程。
class OfflineCodexRunnerTests(unittest.TestCase):
    # 完成、失败、中断和预算停止应返回不同退出码，便于脚本识别真实运行状态。
    def test_command_exit_status_distinguishes_failure_and_stops(self):
        for status, expected in (("completed", 0), ("failed", 1),
                                 ("interrupted", 130), ("budget_stopped", 3), ("running", 1)):
            with self.subTest(status=status):
                self.assertEqual(exit_status({"status": status}), expected)

    # 用离线会话模拟两轮边界，检查检查点和损失账本落盘，并确认探针答案没有回灌主会话。
    def test_two_native_rounds_saved_and_probes_do_not_enter_parent(self):
        from types import SimpleNamespace
        from experiments.llama_cpp.compaction_security.native_retention import codex_runner as runner
        facts = build_corpus(0, 0)['facts']
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)

            # 本用例的离线网关替身，记录临时快照或返回固定答案，不访问真实服务。
            class Gateway:
                local_url = 'http://127.0.0.1:0/v1'
                label = 'offline'
                rows = []

                # 返回离线替身的请求计数或固定用量，供真实编排代码更新进度，不表示真实付费消耗。
                def accounting(self):
                    return {'requests': len(self.rows), 'total_input_tokens': 100,
                            'output_tokens': 10, 'guard_exhausted': False}

                # 返回已记录事件的副本，模拟真实网关的审计接口，避免调用者改动内部账本。
                def events(self):
                    return copy.deepcopy(self.rows)

                # 把伪请求和伪响应写入临时快照，并登记路径和序号，供真实检查点逻辑追溯。
                def capture(self, payload, text):
                    n = len(self.rows) + 1
                    folder = out / 'fake_requests' / str(n)
                    folder.mkdir(parents=True)
                    request = folder / 'request.json'
                    response = folder / 'response.json'
                    request.write_text(json.dumps(payload, ensure_ascii=False))
                    response.write_text(json.dumps({'output': [{'type': 'message', 'content':
                        [{'type': 'output_text', 'text': text}]}]}, ensure_ascii=False))
                    self.rows.append({'idx': n, 'label': self.label, 'status': 200,
                                      'request_path': str(request), 'response_path': str(response),
                                      'response_content_type': 'application/json'})

                # 返回固定真值回答以校准编排记录；这里的人为正确答案不计入真实模型能力或保留率。
                def post(self, request, label):
                    return {'output': [{'type': 'message', 'content': [{'type': 'output_text',
                        'text': json.dumps({'answers': [{'id': f['id'], 'answer': f['answer']} for f in facts]})}]}]}

            gateway = Gateway()
            parents = []

            # 本用例的离线会话替身，用列表模拟主历史和边界，验证编排而不启动 Codex。
            class Session:
                # 为每个离线会话新建历史和边界列表，防止不同轨迹共享模拟状态。
                def __init__(self, *args, **kwargs):
                    self.history = []
                    self.boundaries = []

                # 追加用户输入和“已记录”回复，保存伪请求并收集主历史，以检查探针是否污染主会话。
                def send(self, text):
                    self.history.append(input_message('user', text))
                    gateway.capture({'input': copy.deepcopy(self.history)}, '已记录')
                    self.history.append(input_message('assistant', '已记录'))
                    parents.append(copy.deepcopy(self.history))
                    return {'events': [{'method': 'item/completed', 'params':
                                        {'item': {'type': 'agentMessage', 'text': '已记录'}}}]}

                # 人工记录一次压缩边界或替换为固定摘要，仅用于验证保存流程，不能作为真实压缩结果。
                def compact(self):
                    gateway.capture({'input': copy.deepcopy(self.history)}, 'offline summary')
                    self.boundaries.append({'type': 'contextCompaction'})

                # 返回离线替身已经创建的压缩边界列表，供编排器对比新增次数。
                def compactions(self):
                    return list(self.boundaries)

                # 离线替身没有真实子进程或连接需要释放，保留接口便于编排器统一清理。
                def close(self):
                    pass

            # 以下才启动被测编排器；CodexSession 已被替换为上面的离线 Session。
            args = SimpleNamespace(histories=1, runs=1, thresholds=[24000], source_roles=['user'],
                                   rounds=2, quiet=True, trigger='manual', timeout=10, probes=True,
                                   max_chunks=5, chunk_chars=256)
            provider = {'model': 'offline-model', 'reasoning_effort': 'high'}
            ex = Experiment(args, out, provider, gateway)
            cell = {'id': 'offline_cell', 'history': 0, 'repeat': 0, 'threshold': 24000, 'source_role': 'user'}
            with patch.object(runner, 'CodexSession', Session):
                ex.run([cell])
            # 同时核对过程产物与父历史：文件写出来并不够，还要确认测量没有把答案带回实验。
            self.assertEqual(ex.progress['status'], 'completed')
            self.assertEqual(ex.progress['compactions_completed'], 2)
            self.assertEqual(ex.progress['runs_completed'], 1)
            self.assertTrue((out / 'lost_content.md').exists())
            self.assertTrue((out / 'trajectories/offline_cell/checkpoints/2/measurements.json').exists())
            self.assertTrue(all('"answers"' not in json.dumps(h) for h in parents))
            self.assertIn('2/2', (out / 'report.md').read_text())


if __name__ == '__main__':
    unittest.main()
