import tkinter as tk
from tkinter import filedialog
import threading
from openai import OpenAI
from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By

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

        self.left_frame = tk.Frame(self, bg="white")
        self.left_frame.pack(side="left", fill="both", expand=True)
        
        self.button_image = tk.PhotoImage(file="resources/upload_button.png")

        self.upload_button = tk.Button(self.left_frame, text="", command=self.open_file,
                               font=("Arial", 12, "bold"),
                               fg="white",
                               bg="white",
                               activebackground="lightblue",
                               activeforeground="white",
                               relief="flat",
                               borderwidth=0,
                               padx=10,
                               pady=5,
                               image=self.button_image,
                               width=259,
                               height=202,
                               compound="left")
        self.upload_button.pack(pady=20)

        self.process_label = tk.Label(self.left_frame, text="Select Aadhar image\n * Must be less than 500 Kb", font=("Arial", 12), bg="white")
        self.process_label.pack()

        self.right_frame = tk.Frame(self, bg="white", width=200, padx=18)
        self.right_frame.pack(side="right", fill="both", expand=True)

        self.text_display = tk.Text(self.right_frame, wrap="word", height=10, bg="lightgray", state="disabled")
        self.text_display.pack(pady=20, expand=True, fill="both")

    def open_file(self):
        file_path = filedialog.askopenfilename(initialdir="/", title="Select a file", filetypes=(("all files", "*.*"), ("jpg files", "*.jpg")))
        if file_path:
            self.process_label.config(text="[00%] Processing...")
            threading.Thread(target=self.process_file, args=(file_path,)).start()

    def process_file(self, file_path):
        try:
            class Chat:
                def __init__(self):
                    # Update with your actual ChatGPT API key 
                    Api_key = '**-****-************************************************'
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
