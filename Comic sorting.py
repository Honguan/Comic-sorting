import os
import re
import shutil
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox, Scrollbar

class FileAggregatorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("檔案整合工具")
        self.base_path = ""
        self.folders = []

        # 輸入基礎路徑
        self.path_label = tk.Label(root, text="請選擇包含資料夾的基礎路徑:")
        self.path_label.pack(pady=10)

        self.path_entry = tk.Entry(root, width=50)
        self.path_entry.pack(pady=5)

        self.browse_button = tk.Button(root, text="瀏覽", command=self.browse)
        self.browse_button.pack(pady=5)

        # 顯示資料夾列表
        self.folder_listbox = Listbox(root, selectmode=tk.SINGLE, width=80)
        self.folder_listbox.pack(pady=10)

        self.scrollbar = Scrollbar(root)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.folder_listbox.config(yscrollcommand=self.scrollbar.set)
        self.scrollbar.config(command=self.folder_listbox.yview)

        # 起始和結束選擇框
        self.start_label = tk.Label(root, text="選擇起始資料夾:")
        self.start_label.pack(pady=5)

        self.start_entry = tk.Entry(root, width=5)
        self.start_entry.pack(pady=5)

        self.end_label = tk.Label(root, text="選擇結束資料夾:")
        self.end_label.pack(pady=5)

        self.end_entry = tk.Entry(root, width=5)
        self.end_entry.pack(pady=5)

        # 確認整合按鈕
        self.confirm_button = tk.Button(root, text="確認整合", command=self.confirm_aggregate)
        self.confirm_button.pack(pady=10)

    def browse(self):
        """選擇資料夾並列出資料夾"""
        self.base_path = filedialog.askdirectory()
        if self.base_path:
            self.path_entry.delete(0, tk.END)
            self.path_entry.insert(0, self.base_path)
            self.load_folders()

    def load_folders(self):
        """加載資料夾並顯示在列表中"""
        self.folders = self.get_folders_with_numbers(self.base_path)
        self.folder_listbox.delete(0, tk.END)
        for idx, (folder_path, folder_name, folder_number) in enumerate(self.folders, start=1):
            file_count = len(os.listdir(folder_path))
            self.folder_listbox.insert(tk.END, f"編號 : {idx} - {folder_name} - 檔案數量 : {file_count}")

    def get_folders_with_numbers(self, base_path):
        """取得所有資料夾及其數字編號，並依數字排序"""
        folders = []
        for folder_name in os.listdir(base_path):
            folder_path = os.path.join(base_path, folder_name)
            if os.path.isdir(folder_path):
                try:
                    folder_number = float(re.findall(r'\d+\.?\d*', folder_name)[0])
                    folders.append((folder_path, folder_name, folder_number))
                except (IndexError, ValueError):
                    continue  # 忽略不包含數字的資料夾
        # 依資料夾數字編號排序
        return sorted(folders, key=lambda x: x[2])

    def confirm_aggregate(self):
        """確認整合資料夾"""
        start_idx = self.start_entry.get()
        end_idx = self.end_entry.get()
        
        if not start_idx.isdigit() or not end_idx.isdigit():
            messagebox.showwarning("警告", "請輸入有效的起始和結束編號")
            return

        start_idx = int(start_idx) - 1
        end_idx = int(end_idx) - 1
        
        if start_idx < 0 or end_idx >= len(self.folders) or start_idx > end_idx:
            messagebox.showwarning("警告", "請確保編號範圍有效")
            return
        
        folder_names = [self.folders[i][1] for i in range(start_idx, end_idx + 1)]
        confirmation = messagebox.askyesno("確認整合", f"您確定要整合以下資料夾嗎？\n\n{', '.join(folder_names)}")
        
        if confirmation:
            self.aggregate_folders(start_idx, end_idx)

    def aggregate_folders(self, start_idx, end_idx):
        """整合選擇的資料夾"""
        selected_folders = self.folders[start_idx:end_idx + 1]
        output_folder_name = f"Chapter {self.folders[start_idx][1].split(' ')[1]}-{self.folders[end_idx][1].split(' ')[1]}"
        output_folder = os.path.join(self.base_path, output_folder_name)

        # 建立統整資料夾（若不存在）
        os.makedirs(output_folder, exist_ok=True)

        # 建立 temp 資料夾
        temp_folder = os.path.join(self.base_path, "temp")
        os.makedirs(temp_folder, exist_ok=True)

        # 進行檔案重命名和複製
        self.rename_files_sequentially_and_copy(selected_folders, temp_folder, output_folder)

        # 刪除 temp 資料夾
        shutil.rmtree(temp_folder)

        messagebox.showinfo("完成", f"檔案已重命名並複製到 {output_folder_name}")

    def rename_files_sequentially_and_copy(self, folders, temp_folder, output_folder):
        """依序重命名資料夾中的檔案並複製到統整資料夾"""
        current_index = 1  # 檔案編號從 1 開始
        for folder_path, _, _ in folders:
            files = sorted(os.listdir(folder_path), key=lambda f: int(re.findall(r'\d+', f)[0]))
            for filename in files:
                # 提取檔案副檔名
                ext = os.path.splitext(filename)[1]
                new_filename = f"{current_index}{ext}"
                original_file_path = os.path.join(folder_path, filename)
                temp_file_path = os.path.join(temp_folder, new_filename)

                # 複製原始檔案到 temp 資料夾
                shutil.copy(original_file_path, temp_file_path)

                # 複製重新命名的檔案到統整資料夾
                shutil.copy(temp_file_path, os.path.join(output_folder, new_filename))

                current_index += 1  # 依序增加檔案編號

if __name__ == "__main__":
    root = tk.Tk()
    app = FileAggregatorApp(root)
    root.mainloop()
