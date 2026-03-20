# Sentinel Press Architecture
The core architecture is RAG based. This is to ensure that the AI does not hallucinate and only uses internal data.
This should be a chatbot.

TODO: dimensionality of the vector database needs to be managed by a panel.
UI panel to manage document sources, custom prompts, review, configure models, MCP, etc.

AI to conduct interviews for inclusion in Newsletter. 


````mermaid
zenuml
    title Data Preparation

    @CloudInterconnect Ingestion
    @Sagemaker Classifier
    @Sagemaker PS as "Privacy Scrub"
    @Database Vector
    @Database Meta as "Meta Data"
    
    Ingestion->Ingestion: Filter out Draft
    Ingestion->Classifier: Ingest Meta and Classify
    Classifier->PS: remove PII, sensitive data
    PS->Vector: Store documents as Vector
    PS->Meta: Store the Meta Data

````
###  1. Ingestion
Ingests from various sources with unstructured data which is mostly documents. This phase should use various integrators (already avaiable or custom built)to ingest the metadata as well as document details(if required). It should filter data documents. To determine draft document use the meta data for now and check for the word "draft" or anything similar.

Subcomponents
    a. Document hoarder - An AI agent that can adaptively integrate with document sources and read metadata and documents from them. The metadata would vary based on sources like Confluence, Gdoc, Sharepoint, Slack, Whatsapp, -etc. Checks in Time range, incremental.

        The "Draft" Filter: This is the most critical component. Two possible ways to achieve this:
        Folder Structure: Only sync documents which do NOT have the name DRAFT in its full path
        Metadata Tagging: Filter files where the "Status" property equals "Released" or "Frozen."
        Maintains an audit of document processed and their outcomes.

        Metadata includes
            1. original path of the document
            2. Any tags, date of ingestion, date of document creation/upate, original author[s]
            3. Abstract
            4. Are there images?
            5. Which the front image?
            6. file name, extension, location
            7. Temporal


    b. Screener - Checks for draft status in metadata, 
        For looking up in content for draft versioning and authenticity, try reasoning LLM.

        First check in metadata like name, versioning(ones with now major version).


    C. Standardizer - Massage the document content, convert to text for use by AI, extract and store the metadata, 


###  2. Classifier/segmentizer

 UI to provuide sections for newsletter or AI can auto suggest based on target audience, business
 UI to have companyu profile with products.
    UI to provide hireachy of roles for letters.,
    UI to provide special note from writer



## Newsletter Generator
Need to generate both text and images. Any original images in the documents, if present, should be used.
We need an admin panel to allow to specify sections for the newsletter.
AI should not generate images, unless asked to do so and it should attribute it to itself in the output.
The output should target multiple channels if required.

Auto generated articles should quote from the input docs and have references and mention the quotation author

Only image and text and may be gif should be generated.

TODO:  We need a critic and a reviser similar to the self-story.ts POC



````mermaid
zenuml
    title "News letter generation"

    @Database Vector as "Vectorised Docs"
    @Database Meta as "MetaData"
    @Database Newsletter
    @Sagemaker AgentT as "AI agent for Text and Section"
    @Sagemaker AgentV as "AI Visual Augmentor"
    @Sagemaker AgentR as "AI based reviewer"
    @Actor Reviewer

    while("Looks good to human") {
        while("Looks good to AI") {
            AgentT->Vector: Pull documents for the News letter
            AgentT->Meta: Pull meta for the News letter
            AgentT->Newsletter: Read Feedback
            AgentT->AgentV: Generate Images
            AgentT->Newsletter: Output generated Newsletter
            AgentR->Newsletter: Review Newsletter
            AgentR->Newsletter: Provide Feedback
        }
        Reviewer->Newsletter: Review Newsletter
        Reviewer->Newsletter: Provide Feedback
    }


````
Human in the Loop - To allow review of the generated newsletter


# Safeguards
Only Image or gif and text. Avoid video even if original prompt says so.
Safeguards should override the Reviewer input.

# Testing Strategy
Principles
    Find a framework to follow for testing like accuracy, safeguards, etc.
    Test with various models
    Test against existing benchmarks.
    Test the output matches the specs.
    Newsletters do not repeat themselves and are able to back refer.
    Org announcments or articles from Senior members (AI use the org chart and designation, seniority) to determine the artiles in a particular subject and relevance.

    Query from chatbot interface should be rewritten. Call the component as User Intent 

# Observability


# Assumptions
    Support only for English for now.
    Next scale to Hindi.