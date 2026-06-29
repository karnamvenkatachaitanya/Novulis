-- Part 1: Database Schema Setup
-- Run this in your Supabase SQL Editor to initialize the pgvector table and search functions.

-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Create table for storing guideline text chunks and their embeddings
CREATE TABLE IF NOT EXISTS guideline_embeddings (
    id bigserial PRIMARY KEY,
    section_name text NOT NULL,
    url_path text NOT NULL,
    content text NOT NULL,
    embedding vector(384) NOT NULL
);

-- Create an HNSW index to accelerate cosine similarity search queries
CREATE INDEX IF NOT EXISTS guideline_embeddings_embedding_idx
ON guideline_embeddings
USING hnsw (embedding vector_cosine_ops);

-- Create Hybrid Cosine Similarity Search function (RPC)
CREATE OR REPLACE FUNCTION match_guidelines(
    query_embedding vector(384),
    filter_url_path text DEFAULT NULL,
    similarity_threshold double precision DEFAULT 0.25,
    limit_count integer DEFAULT 5
)
RETURNS TABLE (
    id bigint,
    section_name text,
    url_path text,
    content text,
    similarity double precision
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    SELECT
        ge.id,
        ge.section_name,
        ge.url_path,
        ge.content,
        (1 - (ge.embedding <=> query_embedding))::double precision AS similarity
    FROM
        guideline_embeddings ge
    WHERE
        (filter_url_path IS NULL OR ge.url_path = filter_url_path OR ge.url_path = '/shared')
        AND (1 - (ge.embedding <=> query_embedding)) >= similarity_threshold
    ORDER BY
        ge.embedding <=> query_embedding ASC
    LIMIT
        limit_count;
END;
$$;

-- ===================================================================
-- Part 2: Dashboard Snapshots for RAG Chatbot
-- Stores vectorized chunks from live dashboard scrapes.
-- ===================================================================

-- Table for storing scraped dashboard page chunks and their embeddings
CREATE TABLE IF NOT EXISTS dashboard_snapshots (
    id bigserial PRIMARY KEY,
    page_path text NOT NULL,
    section_label text NOT NULL,
    content text NOT NULL,
    scraped_at timestamptz NOT NULL DEFAULT now(),
    embedding vector(384) NOT NULL
);

CREATE INDEX IF NOT EXISTS dashboard_snapshots_embedding_idx
ON dashboard_snapshots USING hnsw (embedding vector_cosine_ops);

-- RPC function for similarity search on live dashboard snapshots
CREATE OR REPLACE FUNCTION match_dashboard_snapshots(
    query_embedding vector(384),
    filter_page_path text DEFAULT NULL,
    similarity_threshold double precision DEFAULT 0.25,
    limit_count integer DEFAULT 5
)
RETURNS TABLE (
    id bigint,
    page_path text,
    section_label text,
    content text,
    scraped_at timestamptz,
    similarity double precision
)
LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    SELECT
        ds.id, ds.page_path, ds.section_label, ds.content, ds.scraped_at,
        (1 - (ds.embedding <=> query_embedding))::double precision AS similarity
    FROM dashboard_snapshots ds
    WHERE
        (filter_page_path IS NULL OR ds.page_path = filter_page_path)
        AND (1 - (ds.embedding <=> query_embedding)) >= similarity_threshold
    ORDER BY ds.embedding <=> query_embedding ASC
    LIMIT limit_count;
END;
$$;
