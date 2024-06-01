# Aadhar Scanner
A GUI-based application for extracting information from Aadhar cards using Azure Computer Vision and OpenAI's GPT-3.

# Overview
This application uses a combination of Azure Computer Vision and OpenAI's GPT-3 to extract information from Aadhar cards. The user can upload an image of the Aadhar card, and the application will extract the relevant information and display it in a JSON format.

# Features
- Upload an image of an Aadhar card
- Extract information from the image using Azure Computer Vision
- Process the extracted information using OpenAI's GPT-3 to generate a JSON output
- Display the JSON output in a user-friendly format

# How it Works
- The user uploads an image of an Aadhar card using the GUI.
- The application uses Azure Computer Vision to extract the text from the image.
- The extracted text is then processed using OpenAI's GPT-3 to generate a JSON output.
- The JSON output is displayed in a user-friendly format in the GUI.
- Technical Details
- The application uses the tkinter library for the GUI.
- The selenium library is used to interact with the Azure Computer Vision demo website.
- The openai library is used to interact with OpenAI's GPT-3 API.
- The application uses threading to process the file upload and extraction in the background.

# Script
### Modules 
```python
import tkinter as tk
from tkinter import filedialog
import threading
from openai import OpenAI
from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
```

### Main script
```python
options = webdriver.ChromeOptions()
options.add_argument('--headless')

driver = webdriver.Chrome(options=options)
driver.get('https://portal.vision.cognitive.azure.com/demo/extract-text-from-images')

class FileUploader(tk.Tk):
    def __init__(self):
        super().__init__()
        
        self.resizable(False, False)
        self.title("Aadhar Scanner")
        self.geometry("600x400")
        self.configure(bg="white")
        icon = tk.PhotoImage(file='resources/icon.png')
        self.iconphoto(False, icon)

        # Create left frame for file upload
        self.left_frame = tk.Frame(self, bg="white")
        self.left_frame.pack(side="left", fill="both", expand=True)
        
        self.button_image = tk.PhotoImage(file="resources/upload_button.png")

        # File upload button
        self.upload_button = tk.Button(self.left_frame, text="", command=self.open_file,
                               font=("Arial", 12, "bold"),  # font family, size, and style
                               fg="white",  # text color
                               bg="white",  # background color
                               activebackground="lightblue",  # background color on hover
                               activeforeground="white",  # text color on hover
                               relief="flat",  # button relief (flat, raised, sunken, etc.)
                               borderwidth=0,  # button border width
                               padx=10,  # horizontal padding
                               pady=5,
                               image=self.button_image,
                               width=259,
                               height=202,
                               compound="left")  # display image on the left side of the text
        self.upload_button.pack(pady=20)

        # Process display
        self.process_label = tk.Label(self.left_frame, text="Select Aadhar image\n * Must be less than 500 Kb", font=("Arial", 12), bg="white")
        self.process_label.pack()

        # Create right frame for processed text display
        self.right_frame = tk.Frame(self, bg="white", width=200, padx=18)
        self.right_frame.pack(side="right", fill="both", expand=True)

        # Processed text display container
        self.text_display = tk.Text(self.right_frame, wrap="word", height=10, bg="lightgray", state="disabled")
        self.text_display.pack(pady=20, expand=True, fill="both")

    def open_file(self):
        """Opens file dialog and processes the selected file."""
        file_path = filedialog.askopenfilename(initialdir="/", title="Select a file", filetypes=(("all files", "*.*"), ("jpg files", "*.jpg")))
        if file_path:
            # Simulate processing (replace with your actual processing logic)
            self.process_label.config(text="[00%] Processing...")
            threading.Thread(target=self.process_file, args=(file_path,)).start()

    def process_file(self, file_path):
        """Processes the selected file and updates the text display."""
        try:
            # Replace this with your actual processing logic
            class Chat:
                def __init__(self):
                    Api_key = '<replace with your Chat GPT API key>'
                    self.client = OpenAI(api_key=Api_key)
        
                def prompt(self, prompt):
                    completion = self.client.chat.completions.create(
                      model="gpt-3.5-turbo-16k",
                      messages=[
                        {"role": "user", "content": f"{prompt}"}
                      ]
                    )
                    self.response = completion.choices[0]
                    return(completion.choices[0].message.content)

            GPT = Chat()        

            self.process_label.config(text="[07%] Processing...")

            image_upload = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input"))
            )

            image_upload.send_keys(file_path)
            self.process_label.config(text="[10%] Processing...")

            output = WebDriverWait(driver, 10).until(
                EC.text_to_be_present_in_element((By.CLASS_NAME, "demo-list-content"), "India")
            )
            driver.execute_script('''document.querySelector('.demo-list-content').querySelectorAll('span').forEach(text => {
                                         text.innerText = text.innerText + ' ';
                                    })
                                    ''')
            self.process_label.config(text="[22%] Processing...")
                
            reponse = driver.execute_script("return document.querySelector('.demo-list-content').innerText")
            self.process_label.config(text="[60%] Processing...")
    
            prompt = f"Extract details form this raw data in JSON format containing authority, name (hindi and english both), gender, dob, aadhar number, VID (optional), address (optional), Guardian_Name (optional), additional informations like date of issue, download,  punch_line, etc (optional)\n`{reponse}`"
    
            final_JSON = GPT.prompt(prompt)
            self.process_label.config(text="[88%] Processing...")
            print(final_JSON)
    
            with open('request.json','a',encoding='utf-8') as file:
                file.write(final_JSON)
            self.process_label.config(text="[99%] Processing...")
            processed_text = final_JSON

        except Exception as e:
            processed_text = f"Error processing file: {e}"

        # Update text display in the main thread
        self.after(1000, lambda: self.update_text_display(processed_text))

    def update_text_display(self, processed_text):
        """Updates the processed text display container."""
        self.text_display.config(state="normal")
        self.text_display.delete("1.0", tk.END)
        self.text_display.insert(tk.END, processed_text)
        self.text_display.config(state="disabled")
        self.process_label.config(text="Processing complete.")
        driver.refresh()

if __name__ == "__main__":
    app = FileUploader()
    app.mainloop()
```

# Confidential Information
- The OpenAI API key has been removed from this repository for security reasons. You will need to obtain your own API key and replace it in the code.
- The Azure Computer Vision demo website is used for demonstration purposes only. You will need to obtain your own Azure subscription and set up your own Computer Vision resource.

# Contributing
Contributions are welcome! If you'd like to contribute to this project, please fork the repository and submit a pull request.

# Acknowledgments
- Azure Computer Vision for providing the demo website used in this application.
- OpenAI for providing the GPT-3 API used in this application.
