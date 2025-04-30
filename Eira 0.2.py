from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os
import torch
import pdfplumber
from pdf2image import convert_from_path
from transformers import AutoProcessor, BlipForConditionalGeneration, AutoTokenizer, AutoModelForCausalLM
from PIL import Image
from huggingface_hub import login
import re
import speech_recognition as sr
from gtts import gTTS
from io import BytesIO
import base64
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def remove_html_tags(text):
    clean = re.compile('<.*?>')
    return re.sub(clean, '', text).strip()

def extract_answer(text):
    match = re.search(r'Answer:\s*(.*)', text, re.DOTALL)
    return match.group(1).strip() if match else text.strip()

app = Flask(__name__)
CORS(app)

device = "cuda" if torch.cuda.is_available() else "cpu"
logger.info(f"Using device: {device}")

hf_token = "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"
login(token=hf_token)

# Load models
blip_processor = AutoProcessor.from_pretrained("Salesforce/blip-image-captioning-base")
blip_model = BlipForConditionalGeneration.from_pretrained("Salesforce/blip-image-captioning-base").to(device)

llama_model_name = "meta-llama/Llama-2-7b-hf"
llama_tokenizer = AutoTokenizer.from_pretrained(llama_model_name, token=hf_token)
llama_model = AutoModelForCausalLM.from_pretrained(llama_model_name, torch_dtype=torch.float16).to(device)

# Configure paths
pdf_folder = "/home/eiraai_bock/eira_0.2_backend/datasets"
output_folder = "/home/eiraai_bock/eira_0.2_backend/output"
os.makedirs(output_folder, exist_ok=True)

@app.route("/chat", methods=["POST", "OPTIONS"])
def chat():
    if request.method == "OPTIONS":
        return jsonify({"success": True}), 200

    try:
        audio_bytes = BytesIO()
        return_json = True
        user_input = ""
        is_voice = False

        # Handle input types
        if request.content_type == "application/json":
            data = request.get_json()
            user_input = data.get("text", "").strip()
            is_voice = data.get("is_voice", False)
        elif "audio" in request.files:
            audio_file = request.files["audio"]
            recognizer = sr.Recognizer()
            return_json = False
            is_voice = True
            
            with sr.AudioFile(audio_file) as source:
                audio = recognizer.record(source)
            
            user_input = recognizer.recognize_google(audio)
        else:
            return jsonify({
                "success": False,
                "error": "Unsupported content type. Use JSON or audio file."
            }), 400

        if not user_input:
            return jsonify({"success": False, "error": "Empty input"}), 400

        # Generate response
        input_text = f"User Query: {user_input}\n\nAnswer:"
        inputs = llama_tokenizer(input_text, return_tensors="pt", truncation=True, max_length=512).to(device)
        
        with torch.no_grad():
            outputs = llama_model.generate(**inputs, max_new_tokens=500)
        
        response_text = llama_tokenizer.decode(outputs[0], skip_special_tokens=True)
        final_answer = extract_answer(remove_html_tags(response_text))
        
        # Generate audio
        audio_base64 = None
        if is_voice:
            try:
                tts = gTTS(final_answer, lang="en")
                tts.write_to_fp(audio_bytes)
                audio_bytes.seek(0)
                
                if return_json:
                    audio_base64 = base64.b64encode(audio_bytes.read()).decode("utf-8")
            except Exception as e:
                logger.error(f"Audio error: {str(e)}")
                return jsonify({
                    "success": True,
                    "text": final_answer,
                    "audio": None,
                    "warning": "Audio generation failed"
                })

        torch.cuda.empty_cache()

        if return_json:
            return jsonify({
                "success": True,
                "text": final_answer,
                "audio": audio_base64
            })
        else:
            return send_file(
                audio_bytes,
                mimetype="audio/mpeg",
                as_attachment=False
            )

    except sr.UnknownValueError:
        return jsonify({"success": False, "error": "Could not understand audio"}), 400
    except sr.RequestError as e:
        return jsonify({"success": False, "error": f"Speech service error: {str(e)}"}), 500
    except Exception as e:
        logger.error(f"Chat error: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/process_pdfs", methods=["POST"])
def process_pdfs():
    try:
        pdf_files = [f for f in os.listdir(pdf_folder) if f.endswith(".pdf")]
        if not pdf_files:
            return jsonify({"success": False, "error": "No PDFs found"}), 404

        extracted_data = {}
        for pdf_file in pdf_files[:2]:  # Process first 2 PDFs
            pdf_path = os.path.join(pdf_folder, pdf_file)
            text_output = []
            image_paths = []

            try:
                with pdfplumber.open(pdf_path) as pdf:
                    for page in pdf.pages:
                        text = page.extract_text()
                        if text: text_output.append(text)

                images = convert_from_path(pdf_path)
                for idx, img in enumerate(images[:5]):
                    image_path = os.path.join(output_folder, f"{pdf_file}_page{idx+1}.png")
                    img.save(image_path, "PNG")
                    image_paths.append(image_path)

                extracted_data[pdf_file] = {
                    "text": "\n".join(text_output),
                    "images": image_paths
                }
            except Exception as e:
                extracted_data[pdf_file] = {"error": f"Processing error: {str(e)}"}

        return jsonify({"success": True, "data": extracted_data})

    except Exception as e:
        logger.error(f"PDF error: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/generate_caption", methods=["POST"])
def generate_caption():
    try:
        data = request.json
        image_paths = data.get("image_paths", [])
        if not image_paths:
            return jsonify({"success": False, "error": "No images provided"}), 400

        captions = {}
        for img_path in image_paths:
            try:
                image = Image.open(img_path).convert("RGB")
                inputs = blip_processor(images=image, return_tensors="pt").to(device)
                with torch.no_grad():
                    output = blip_model.generate(**inputs)
                captions[img_path] = blip_processor.batch_decode(output, skip_special_tokens=True)[0]
            except Exception as e:
                captions[img_path] = f"Error: {str(e)}"

        return jsonify({"success": True, "captions": captions})

    except Exception as e:
        logger.error(f"Caption error: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/query", methods=["POST"])
def query():
    try:
        data = request.json
        query_text = data.get("query", "").strip()
        context = data.get("context", "").strip()

        if not query_text:
            return jsonify({"success": False, "error": "Empty query"}), 400

        input_text = f"Context:\n{context}\n\nQuery: {query_text}\n\nAnswer:"
        inputs = llama_tokenizer(input_text, return_tensors="pt", truncation=True, max_length=512).to(device)

        with torch.no_grad():
            outputs = llama_model.generate(**inputs, max_new_tokens=500)

        response_text = llama_tokenizer.decode(outputs[0], skip_special_tokens=True)
        final_answer = extract_answer(remove_html_tags(response_text))
        
        return jsonify({"success": True, "response": final_answer})

    except Exception as e:
        logger.error(f"Query error: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/speech_to_text", methods=["POST"])
def speech_to_text():
    try:
        if 'file' not in request.files:
            return jsonify({"success": False, "error": "No audio file"}), 400

        audio_file = request.files['file']
        recognizer = sr.Recognizer()

        with sr.AudioFile(audio_file) as source:
            audio = recognizer.record(source)

        text = recognizer.recognize_google(audio)
        return jsonify({"success": True, "text": text})

    except sr.UnknownValueError:
        return jsonify({"success": False, "error": "Audio not understood"}), 400
    except Exception as e:
        logger.error(f"STT error: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/text_to_speech", methods=["POST"])
def text_to_speech():
    try:
        data = request.json
        text = data.get("text", "").strip()
        if not text:
            return jsonify({"success": False, "error": "Empty text"}), 400

        audio_bytes = BytesIO()
        tts = gTTS(text, lang="en")
        tts.write_to_fp(audio_bytes)
        audio_bytes.seek(0)

        return send_file(
            audio_bytes,
            mimetype="audio/mpeg",
            as_attachment=False
        )

    except Exception as e:
        logger.error(f"TTS error: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)