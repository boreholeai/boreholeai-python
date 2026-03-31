from boreholeai import BoreholeAI, InsufficientCreditsError, AuthenticationError

client = BoreholeAI(api_key="bhai_123")

result = client.process_documents("Borehole.pdf")