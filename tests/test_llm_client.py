import os
import sys
from unittest.mock import patch, MagicMock

# Add parent directory to path to allow importing from the main package
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from llm_client import LLMClient


def test_llm_client_github_mode():
    """Test creating an LLMClient with GitHub API host."""
    with patch.dict(os.environ, {
        'API_HOST': 'github',
        'GITHUB_TOKEN': 'test-token',
        'GITHUB_MODEL': 'gpt-4o'
    }):
        client = LLMClient()
        assert client.agent is not None


def test_llm_client_azure_mode():
    """Test creating an LLMClient with Azure OpenAI (mocked)."""
    with patch.dict(os.environ, {
        'API_HOST': 'azure',
        'AZURE_OPENAI_ENDPOINT': 'https://test.openai.azure.com',
        'AZURE_OPENAI_CHAT_DEPLOYMENT': 'gpt-4o'
    }):
        # Mock Azure credential and token provider
        with patch('azure.identity.DefaultAzureCredential') as mock_cred, \
             patch('azure.identity.get_bearer_token_provider') as mock_token_provider:
            
            mock_token_provider.return_value = MagicMock()
            
            client = LLMClient()
            assert client.agent is not None
            
            # Verify the token provider was called with correct parameters
            mock_token_provider.assert_called_once()
            args, kwargs = mock_token_provider.call_args
            assert "https://cognitiveservices.azure.com/.default" in args


def test_llm_client_unknown_api_host():
    """Test that LLMClient raises RuntimeError for unknown API_HOST."""
    with patch.dict(os.environ, {'API_HOST': 'unknown'}):
        try:
            LLMClient()
            assert False, "Should have raised RuntimeError"
        except RuntimeError as e:
            assert str(e) == "Unknown API_HOST"