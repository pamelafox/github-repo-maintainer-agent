import os

from openai import AsyncAzureOpenAI, AsyncOpenAI
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from models import AnalyzeFailureInput, AnalyzeFailureOutput


class LLMClient:
    def __init__(self):
        api_host = os.getenv("API_HOST", "github")
        if api_host == "github":
            client = AsyncOpenAI(api_key=os.environ["GITHUB_TOKEN"], base_url="https://models.inference.ai.azure.com")
            model = OpenAIModel(os.getenv("GITHUB_MODEL", "gpt-4o"), provider=OpenAIProvider(openai_client=client))
        elif api_host == "azure":
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider
            token_provider = get_bearer_token_provider(DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
            client = AsyncAzureOpenAI(
                api_version=os.environ["AZURE_OPENAI_VERSION"],
                azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
                azure_ad_token_provider=token_provider,
            )
            model = OpenAIModel(os.environ["AZURE_OPENAI_CHAT_DEPLOYMENT"], provider=OpenAIProvider(openai_client=client))
        else:
            raise RuntimeError("Unknown API_HOST")
        self.agent = Agent(
            model,
            system_prompt="""
You are an expert at analyzing GitHub Actions workflow failures for Dependabot PRs. Given a list of check runs and a PR URL, categorize the failure type, summarize the root cause with a few paragraphs and any relevant details (especially about package conflicts), and extract any relevant error log lines. Be specific about the packages that are conflicting - name the packages and versions.
""",
            output_type=NativeOutput(AnalyzeFailureOutput),
        )

    async def analyze_failure(self, input: AnalyzeFailureInput) -> AnalyzeFailureOutput:
        # Convert to dict with native types (e.g., HttpUrl to str)
        input_dict = input.model_dump(mode="json")
        return (await self.agent.run(input_dict)).output
