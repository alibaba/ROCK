import {
  ModelInfo,
  ModelInfoSchema,
  AgentInfo,
  AgentInfoSchema,
  AgentResult,
  AgentResultSchema,
  VerifierResult,
  VerifierResultSchema,
  TimingInfo,
  TimingInfoSchema,
  ExceptionInfo,
  ExceptionInfoSchema,
  HarborTrialResult,
  HarborTrialResultSchema,
  createHarborTrialResultFromJson,
} from './result';

// ---------------------------------------------------------------------------
// ModelInfo
// ---------------------------------------------------------------------------
describe('ModelInfo', () => {
  describe('ModelInfoSchema', () => {
    test('parses empty object with defaults', () => {
      const result = ModelInfoSchema.parse({});
      expect(result.name).toBe('');
      expect(result.provider).toBe('');
    });

    test('parses model info', () => {
      const result = ModelInfoSchema.parse({ name: 'gpt-4', provider: 'openai' });
      expect(result.name).toBe('gpt-4');
      expect(result.provider).toBe('openai');
    });
  });
});

// ---------------------------------------------------------------------------
// AgentInfo
// ---------------------------------------------------------------------------
describe('AgentInfo', () => {
  describe('AgentInfoSchema', () => {
    test('parses empty object with defaults', () => {
      const result = AgentInfoSchema.parse({});
      expect(result.name).toBe('');
      expect(result.version).toBe('');
      expect(result.model_info).toBeNull();
    });

    test('parses with model info', () => {
      const result = AgentInfoSchema.parse({
        name: 'test-agent',
        version: '1.0',
        model_info: { name: 'gpt-4', provider: 'openai' },
      });
      expect(result.name).toBe('test-agent');
      expect(result.version).toBe('1.0');
      expect(result.model_info).toEqual({ name: 'gpt-4', provider: 'openai' });
    });
  });
});

// ---------------------------------------------------------------------------
// AgentResult
// ---------------------------------------------------------------------------
describe('AgentResult', () => {
  describe('AgentResultSchema', () => {
    test('parses empty object with null defaults', () => {
      const result = AgentResultSchema.parse({});
      expect(result.n_input_tokens).toBeNull();
      expect(result.n_cache_tokens).toBeNull();
      expect(result.n_output_tokens).toBeNull();
      expect(result.cost_usd).toBeNull();
      expect(result.rollout_details).toBeNull();
    });

    test('parses agent result', () => {
      const result = AgentResultSchema.parse({
        n_input_tokens: 100,
        n_output_tokens: 50,
        cost_usd: 0.02,
      });
      expect(result.n_input_tokens).toBe(100);
      expect(result.n_output_tokens).toBe(50);
      expect(result.cost_usd).toBe(0.02);
    });
  });
});

// ---------------------------------------------------------------------------
// VerifierResult
// ---------------------------------------------------------------------------
describe('VerifierResult', () => {
  describe('VerifierResultSchema', () => {
    test('parses empty object with null rewards', () => {
      const result = VerifierResultSchema.parse({});
      expect(result.rewards).toBeNull();
    });

    test('parses verifier result with rewards', () => {
      const result = VerifierResultSchema.parse({
        rewards: { reward: 0.85, accuracy: 0.9 },
      });
      expect(result.rewards).toEqual({ reward: 0.85, accuracy: 0.9 });
    });
  });
});

// ---------------------------------------------------------------------------
// TimingInfo
// ---------------------------------------------------------------------------
describe('TimingInfo', () => {
  describe('TimingInfoSchema', () => {
    test('parses empty object with null defaults', () => {
      const result = TimingInfoSchema.parse({});
      expect(result.started_at).toBeNull();
      expect(result.finished_at).toBeNull();
    });

    test('parses timing info', () => {
      const result = TimingInfoSchema.parse({
        started_at: '2024-01-01T00:00:00Z',
        finished_at: '2024-01-01T01:00:00Z',
      });
      expect(result.started_at).toBe('2024-01-01T00:00:00Z');
      expect(result.finished_at).toBe('2024-01-01T01:00:00Z');
    });
  });
});

// ---------------------------------------------------------------------------
// ExceptionInfo
// ---------------------------------------------------------------------------
describe('ExceptionInfo', () => {
  describe('ExceptionInfoSchema', () => {
    test('parses empty object with defaults', () => {
      const result = ExceptionInfoSchema.parse({});
      expect(result.exception_type).toBe('');
      expect(result.exception_message).toBe('');
      expect(result.exception_traceback).toBe('');
      expect(result.occurred_at).toBeNull();
    });

    test('parses exception info', () => {
      const result = ExceptionInfoSchema.parse({
        exception_type: 'ValueError',
        exception_message: 'Something went wrong',
        exception_traceback: 'Traceback...',
        occurred_at: '2024-01-01T00:00:00Z',
      });
      expect(result.exception_type).toBe('ValueError');
      expect(result.exception_message).toBe('Something went wrong');
      expect(result.exception_traceback).toBe('Traceback...');
      expect(result.occurred_at).toBe('2024-01-01T00:00:00Z');
    });
  });
});

// ---------------------------------------------------------------------------
// HarborTrialResult
// ---------------------------------------------------------------------------
describe('HarborTrialResult', () => {
  describe('HarborTrialResultSchema', () => {
    test('parses empty object with defaults', () => {
      const result = HarborTrialResultSchema.parse({});
      expect(result.task_name).toBe('');
      expect(result.trial_name).toBe('');
      expect(result.source).toBeNull();
      expect(result.agent_info.name).toBe('');
      expect(result.agent_result).toBeNull();
      expect(result.verifier_result).toBeNull();
      expect(result.exception_info).toBeNull();
      expect(result.started_at).toBeNull();
      expect(result.finished_at).toBeNull();
      expect(result.raw_output).toBe('');
      expect(result.exit_code).toBe(0);
      expect(result.environment_setup).toBeNull();
      expect(result.agent_setup).toBeNull();
      expect(result.agent_execution).toBeNull();
      expect(result.verifier).toBeNull();
    });

    test('parses full trial result', () => {
      const result = HarborTrialResultSchema.parse({
        task_name: 'test-task',
        trial_name: 'test-trial-1',
        source: 'registry',
        agent_info: { name: 'test-agent', version: '1.0' },
        agent_result: { n_input_tokens: 100, cost_usd: 0.01 },
        verifier_result: { rewards: { reward: 0.95 } },
        exception_info: null,
        started_at: '2024-01-01T00:00:00Z',
        finished_at: '2024-01-01T00:05:00Z',
        raw_output: 'execution log...',
        exit_code: 0,
        environment_setup: { started_at: '...', finished_at: '...' },
        agent_setup: { started_at: '...', finished_at: '...' },
        agent_execution: { started_at: '...', finished_at: '...' },
        verifier: { started_at: '...', finished_at: '...' },
      });
      expect(result.task_name).toBe('test-task');
      expect(result.trial_name).toBe('test-trial-1');
      expect(result.verifier_result?.rewards).toEqual({ reward: 0.95 });
    });
  });

  describe('score property', () => {
    test('returns 0.0 when no verifier result', () => {
      const result = HarborTrialResultSchema.parse({});
      expect(result.score).toBe(0.0);
    });

    test('returns 0.0 when verifier result has no rewards', () => {
      const result = HarborTrialResultSchema.parse({
        verifier_result: { rewards: null },
      });
      expect(result.score).toBe(0.0);
    });

    test('returns reward from verifier result', () => {
      const result = HarborTrialResultSchema.parse({
        verifier_result: { rewards: { reward: 0.85 } },
      });
      expect(result.score).toBe(0.85);
    });

    test('returns 0.0 when reward key is missing', () => {
      const result = HarborTrialResultSchema.parse({
        verifier_result: { rewards: { accuracy: 0.9 } },
      });
      expect(result.score).toBe(0.0);
    });
  });

  describe('status property', () => {
    test('returns "completed" when no exception info', () => {
      const result = HarborTrialResultSchema.parse({});
      expect(result.status).toBe('completed');
    });

    test('returns "failed" when exception info present', () => {
      const result = HarborTrialResultSchema.parse({
        exception_info: { exception_type: 'Error', exception_message: 'fail' },
      });
      expect(result.status).toBe('failed');
    });
  });

  describe('token_ids property', () => {
    test('returns empty array when no agent result', () => {
      const result = HarborTrialResultSchema.parse({});
      expect(result.token_ids).toEqual([]);
    });

    test('returns empty array when no rollout details', () => {
      const result = HarborTrialResultSchema.parse({
        agent_result: { rollout_details: null },
      });
      expect(result.token_ids).toEqual([]);
    });

    test('extracts token ids from rollout details', () => {
      const result = HarborTrialResultSchema.parse({
        agent_result: {
          rollout_details: [
            { completion_token_ids: [1, 2, 3] },
            { completion_token_ids: [4, 5] },
          ],
        },
      });
      expect(result.token_ids).toEqual([1, 2, 3, 4, 5]);
    });
  });

  describe('duration_sec property', () => {
    test('returns 0.0 when no timing info', () => {
      const result = HarborTrialResultSchema.parse({});
      expect(result.duration_sec).toBe(0.0);
    });

    test('computes duration from ISO timestamps', () => {
      const result = HarborTrialResultSchema.parse({
        started_at: '2024-01-01T00:00:00Z',
        finished_at: '2024-01-01T00:05:30Z',
      });
      expect(result.duration_sec).toBe(330); // 5 min 30 sec
    });
  });

  describe('createHarborTrialResultFromJson', () => {
    test('parses minimal harbor JSON', () => {
      const result = createHarborTrialResultFromJson({
        task_name: 'test-task',
        trial_name: 'trial-1',
      });
      expect(result.task_name).toBe('test-task');
      expect(result.trial_name).toBe('trial-1');
    });

    test('parses full harbor JSON', () => {
      const result = createHarborTrialResultFromJson({
        task_name: 'test-task',
        trial_name: 'trial-1',
        source: 'registry',
        agent_info: {
          name: 'test-agent',
          version: '1.0',
          model_info: { name: 'gpt-4', provider: 'openai' },
        },
        agent_result: {
          n_input_tokens: 100,
          n_output_tokens: 50,
          cost_usd: 0.02,
          rollout_details: [{ completion_token_ids: [1, 2, 3] }],
        },
        verifier_result: {
          rewards: { reward: 0.95, accuracy: 0.9 },
        },
        exception_info: null,
        started_at: '2024-01-01T00:00:00Z',
        finished_at: '2024-01-01T00:05:00Z',
        environment_setup: { started_at: '...', finished_at: '...' },
        agent_setup: { started_at: '...', finished_at: '...' },
        agent_execution: { started_at: '...', finished_at: '...' },
        verifier: { started_at: '...', finished_at: '...' },
      });
      expect(result.task_name).toBe('test-task');
      expect(result.agent_info.name).toBe('test-agent');
      expect(result.agent_info.model_info?.name).toBe('gpt-4');
      expect(result.agent_result?.n_input_tokens).toBe(100);
      expect(result.verifier_result?.rewards).toEqual({ reward: 0.95, accuracy: 0.9 });
      expect(result.exception_info).toBeNull();
    });

    test('handles string-only exception info', () => {
      const result = createHarborTrialResultFromJson({
        task_name: 'test-task',
        trial_name: 'trial-1',
        exception_info: 'Something went wrong',
      });
      expect(result.exception_info).not.toBeNull();
      expect(result.exception_info?.exception_type).toBe('unknown');
      expect(result.exception_info?.exception_message).toBe('Something went wrong');
    });

    test('handles missing optional fields', () => {
      const result = createHarborTrialResultFromJson({
        task_name: 'test-task',
        trial_name: 'trial-1',
      });
      // No agent_info -> default
      expect(result.agent_info.name).toBe('');
      // No agent_result -> null
      expect(result.agent_result).toBeNull();
      // No verifier_result -> null
      expect(result.verifier_result).toBeNull();
      // No timing -> null
      expect(result.environment_setup).toBeNull();
    });
  });
});
