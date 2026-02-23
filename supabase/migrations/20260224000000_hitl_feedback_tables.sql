-- =============================================================================
-- HITL Feedback Tables
-- Ported from nexus-ai to support Human-in-the-Loop feedback loop
-- =============================================================================

-- Training Samples: stores original + approved responses for fine-tuning
CREATE TABLE IF NOT EXISTS public.training_samples (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    ticket_id VARCHAR(50) NOT NULL,
    analysis_id UUID REFERENCES public.analysis_runs(id) ON DELETE SET NULL,

    -- AI output vs agent-approved output
    original_response JSONB NOT NULL DEFAULT '{}'::jsonb,
    approved_response JSONB,

    -- Metadata
    rating INTEGER CHECK (rating >= 1 AND rating <= 5),
    feedback_text TEXT,
    agent_id VARCHAR(100),
    prompt_version VARCHAR(50),
    model VARCHAR(100),

    -- Export tracking
    is_exported BOOLEAN NOT NULL DEFAULT FALSE,
    exported_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- One training sample per tenant+ticket+analysis
    CONSTRAINT uq_training_sample UNIQUE (tenant_id, ticket_id, analysis_id)
);

-- Indexes for training_samples
CREATE INDEX IF NOT EXISTS idx_training_samples_tenant
    ON public.training_samples(tenant_id);
CREATE INDEX IF NOT EXISTS idx_training_samples_exportable
    ON public.training_samples(tenant_id, is_exported)
    WHERE approved_response IS NOT NULL AND is_exported = FALSE;
CREATE INDEX IF NOT EXISTS idx_training_samples_analysis
    ON public.training_samples(analysis_id);

-- RLS for training_samples
ALTER TABLE public.training_samples ENABLE ROW LEVEL SECURITY;

CREATE POLICY "training_samples_tenant_isolation" ON public.training_samples
    FOR ALL
    USING (tenant_id::text = current_setting('app.current_tenant', true));

CREATE POLICY "training_samples_service_role" ON public.training_samples
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);


-- Feedback Events: event sourcing pattern for all feedback actions
CREATE TABLE IF NOT EXISTS public.feedback_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_id UUID REFERENCES public.analysis_runs(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    event_type VARCHAR(50) NOT NULL,  -- helpful, not_helpful, edited
    agent_id VARCHAR(100),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes for feedback_events
CREATE INDEX IF NOT EXISTS idx_feedback_events_analysis
    ON public.feedback_events(analysis_id);
CREATE INDEX IF NOT EXISTS idx_feedback_events_tenant
    ON public.feedback_events(tenant_id);
CREATE INDEX IF NOT EXISTS idx_feedback_events_type
    ON public.feedback_events(event_type);

-- RLS for feedback_events
ALTER TABLE public.feedback_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY "feedback_events_tenant_isolation" ON public.feedback_events
    FOR ALL
    USING (tenant_id::text = current_setting('app.current_tenant', true));

CREATE POLICY "feedback_events_service_role" ON public.feedback_events
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);


-- Quality Logs: tracks quality metrics and issues
CREATE TABLE IF NOT EXISTS public.quality_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    analysis_id UUID REFERENCES public.analysis_runs(id) ON DELETE SET NULL,
    event_type VARCHAR(50) NOT NULL,  -- language_mismatch, not_helpful, edited, etc.
    agent_id VARCHAR(100),
    detected_language VARCHAR(20),
    response_language VARCHAR(20),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes for quality_logs
CREATE INDEX IF NOT EXISTS idx_quality_logs_tenant
    ON public.quality_logs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_quality_logs_type
    ON public.quality_logs(event_type);

-- RLS for quality_logs
ALTER TABLE public.quality_logs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "quality_logs_tenant_isolation" ON public.quality_logs
    FOR ALL
    USING (tenant_id::text = current_setting('app.current_tenant', true));

CREATE POLICY "quality_logs_service_role" ON public.quality_logs
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);
