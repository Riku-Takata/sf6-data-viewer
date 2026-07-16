-- ============================================================
-- Conversation-scoped tactical knowledge (ADR-026)
--
-- This migration is additive and intentionally creates NO public read policy.
-- The current Discord integration talks to these tables only through the
-- application gateway/service account with an HMAC-derived subject key.
-- Do not apply this migration together with a public SELECT policy.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Stable, non-reversible application identity.  Discord/user IDs must never
-- be stored here; the bot derives subject_key with an operator-controlled HMAC.
CREATE TABLE IF NOT EXISTS knowledge_subjects (
  subject_key     text PRIMARY KEY,
  platform        text NOT NULL,
  status          text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  deleted_at      timestamptz
);

-- Raw chat text is deliberately absent.  We persist only an irreversible hash
-- and a redacted excerpt after the user confirms private saving.
CREATE TABLE IF NOT EXISTS knowledge_turns (
  id                    uuid PRIMARY KEY,
  conversation_id       text NOT NULL,
  speaker_subject_key   text NOT NULL REFERENCES knowledge_subjects(subject_key),
  raw_text_sha256       text NOT NULL CHECK (length(raw_text_sha256) = 64),
  redacted_excerpt      text NOT NULL CHECK (length(redacted_excerpt) <= 500),
  retention_expires_at  timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_knowledge_turns_owner_conversation
  ON knowledge_turns (speaker_subject_key, conversation_id, created_at DESC);

CREATE TABLE IF NOT EXISTS knowledge_consents (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  subject_key           text NOT NULL REFERENCES knowledge_subjects(subject_key),
  consent_kind          text NOT NULL CHECK (consent_kind IN (
                          'private_memory', 'share_request', 'external_llm'
                        )),
  granted               boolean NOT NULL,
  source_turn_id        uuid REFERENCES knowledge_turns(id) ON DELETE SET NULL,
  created_at            timestamptz NOT NULL DEFAULT now(),
  withdrawn_at          timestamptz
);

CREATE INDEX IF NOT EXISTS idx_knowledge_consents_subject_kind
  ON knowledge_consents (subject_key, consent_kind, created_at DESC);

-- scenario_key hashes only identifying conditions, never the claimed result.
-- Thus +7F and +9F reports under identical conditions become a conflict set.
CREATE TABLE IF NOT EXISTS knowledge_claims (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_subject_key       text NOT NULL REFERENCES knowledge_subjects(subject_key),
  scenario_key            text NOT NULL CHECK (length(scenario_key) = 64),
  scenario                jsonb NOT NULL DEFAULT '{}'::jsonb,
  claim_kind              text NOT NULL CHECK (claim_kind IN (
                            'sequence_observation', 'post_trade_advantage',
                            'confirmed_followup', 'spatial_outcome',
                            'tactical_pattern', 'counterplay', 'alias'
                          )),
  payload                 jsonb NOT NULL DEFAULT '{}'::jsonb,
  polarity                text NOT NULL CHECK (polarity IN ('affirmed', 'negated')),
  epistemic_basis         text NOT NULL CHECK (epistemic_basis IN (
                            'firsthand_observation', 'asserted_report', 'hypothesis',
                            'hearsay', 'subjective_preference'
                          )),
  evidence_type           text NOT NULL DEFAULT 'user_report',
  source_turn_id          uuid NOT NULL REFERENCES knowledge_turns(id) ON DELETE RESTRICT,
  raw_text_sha256         text NOT NULL CHECK (length(raw_text_sha256) = 64),
  redacted_excerpt        text NOT NULL CHECK (length(redacted_excerpt) <= 500),
  critical_unknowns       jsonb NOT NULL DEFAULT '[]'::jsonb,
  injection_flags         jsonb NOT NULL DEFAULT '[]'::jsonb,
  workflow_state          text NOT NULL CHECK (workflow_state IN (
                            'private_candidate', 'confirmed_private', 'review_pending',
                            'approved_shared', 'rejected', 'quarantined'
                          )),
  validity_state          text NOT NULL DEFAULT 'active' CHECK (validity_state IN (
                            'active', 'disputed', 'stale_patch', 'superseded',
                            'withdrawn', 'deleted'
                          )),
  visibility_scope        text NOT NULL CHECK (visibility_scope IN ('private', 'community')),
  previous_revision_id    uuid REFERENCES knowledge_claims(id) ON DELETE SET NULL,
  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now(),
  CHECK (jsonb_typeof(scenario) = 'object'),
  CHECK (jsonb_typeof(payload) = 'object'),
  CHECK (jsonb_typeof(critical_unknowns) = 'array'),
  CHECK (jsonb_typeof(injection_flags) = 'array'),
  CHECK (
    (workflow_state = 'approved_shared' AND visibility_scope = 'community')
    OR workflow_state <> 'approved_shared'
  ),
  CHECK (
    visibility_scope = 'private'
    OR workflow_state IN ('review_pending', 'approved_shared', 'rejected', 'quarantined')
  )
);

CREATE INDEX IF NOT EXISTS idx_knowledge_claims_private_lookup
  ON knowledge_claims (owner_subject_key, scenario_key, workflow_state, validity_state);
CREATE INDEX IF NOT EXISTS idx_knowledge_claims_shared_lookup
  ON knowledge_claims (scenario_key, workflow_state, validity_state, visibility_scope);
CREATE INDEX IF NOT EXISTS idx_knowledge_claims_scenario
  ON knowledge_claims USING gin (scenario);

CREATE TABLE IF NOT EXISTS knowledge_evidence (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  claim_id              uuid NOT NULL REFERENCES knowledge_claims(id) ON DELETE CASCADE,
  relation              text NOT NULL CHECK (relation IN ('supports', 'refutes')),
  evidence_kind         text NOT NULL CHECK (evidence_kind IN (
                          'user_report', 'frame_step_video', 'developer_reproduction',
                          'official_source', 'other'
                        )),
  protocol              text,
  asset_sha256          text,
  storage_path          text,
  independence_group    text,
  created_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_knowledge_evidence_claim
  ON knowledge_evidence (claim_id, relation, evidence_kind);

CREATE TABLE IF NOT EXISTS knowledge_claim_relations (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  from_claim_id         uuid NOT NULL REFERENCES knowledge_claims(id) ON DELETE CASCADE,
  to_claim_id           uuid NOT NULL REFERENCES knowledge_claims(id) ON DELETE CASCADE,
  relation              text NOT NULL CHECK (relation IN (
                          'supports', 'refutes', 'duplicates', 'corrects',
                          'supersedes', 'disputes'
                        )),
  created_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (from_claim_id, to_claim_id, relation),
  CHECK (from_claim_id <> to_claim_id)
);

CREATE TABLE IF NOT EXISTS knowledge_reviews (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  claim_id              uuid NOT NULL REFERENCES knowledge_claims(id) ON DELETE CASCADE,
  reviewer_subject_key  text NOT NULL REFERENCES knowledge_subjects(subject_key),
  decision              text NOT NULL CHECK (decision IN ('approved', 'rejected', 'needs_evidence')),
  evidence_kind         text NOT NULL CHECK (evidence_kind IN (
                          'frame_step_video', 'developer_reproduction', 'official_source'
                        )),
  checklist             jsonb NOT NULL DEFAULT '{}'::jsonb,
  reason                text,
  created_at            timestamptz NOT NULL DEFAULT now(),
  CHECK (jsonb_typeof(checklist) = 'object')
);

CREATE INDEX IF NOT EXISTS idx_knowledge_reviews_claim
  ON knowledge_reviews (claim_id, created_at DESC);

CREATE TABLE IF NOT EXISTS knowledge_answer_audit (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  requester_subject_key text NOT NULL REFERENCES knowledge_subjects(subject_key),
  claim_id              uuid REFERENCES knowledge_claims(id) ON DELETE SET NULL,
  scenario_key          text,
  usage_label           text NOT NULL CHECK (usage_label IN (
                          'private_memory', 'reviewed_shared', 'conflict_excluded'
                        )),
  created_at            timestamptz NOT NULL DEFAULT now()
);

-- No anon/authenticated policies are created.  The existing single Bearer MCP
-- must not be granted direct access to these tables.  A future subject-JWT
-- gateway may add owner/reviewer policies without weakening this default.
ALTER TABLE knowledge_subjects       ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_turns          ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_consents       ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_claims         ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_evidence       ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_claim_relations ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_reviews        ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_answer_audit   ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE knowledge_claims IS
  'ADR-026 typed tactical knowledge. Private by default; only approved shared claims may be globally retrieved.';
