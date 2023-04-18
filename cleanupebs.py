import sys
import boto3
from datetime import datetime
import json
ec2 = boto3.client('ec2')
dynamodb = boto3.client('dynamodb')
sns = boto3.client('sns')

class Cleanup:
    
    def find_unused_ebs_volumes(self):
        volumeId = []
        response = ec2.describe_volumes(
                        Filters=[
                            {
                                'Name': 'status',
                                'Values': [
                                            'available',
                                        ]
                            },
                        ],
                        DryRun=False,
                        MaxResults=100,
                    )
        for volId in response['Volumes']:
            ebsRequiredorTagged = False
            try:
                tags = volId['Tags']
                for tag in tags:
                   if tag['Key'] == 'DoNotDelete' or tag['Key'] == 'Delete':
                      ebsRequiredorTagged = True
            except Exception as e:
                print("error getting tag details of volumes %s", e)
            
            if not ebsRequiredorTagged:
               volumeId.append(volId['VolumeId'])
        
        return volumeId

    #update dunamodb table with the list of volumeId'tagged to be deleted
    def update_dynamodb(self,volumeId):
        response = dynamodb.put_item(
            TableName='cleanup',
            Item={'volumeId':{'S':volumeId},'updated':{'S':datetime.today().strftime('%m/%d/%y %H:%M:%S')}}
        )
        return response['ResponseMetadata']['HTTPStatusCode']
        
    #Check the table items if the ebs resource is avaialble and still needs to be deleted
    #Then delete the ebs and remove the entry from table
    def validate_dynamodb_entries(self):
        listofdel = []
        dynamodbItems = dynamodb.scan(
            TableName='cleanup'
            )
        
        for items in dynamodbItems['Items']:
            vol = items['volumeId']['S']
            updated = items['updated']['S']
            #get tag details
            try:
                tagResponse = ec2.describe_volumes(
                        VolumeIds=[vol],
                        DryRun=False,
                )
                tags = tagResponse['Volumes'][0]['Tags']
            except Exception as e:
                print("volume not found ", e, " removing entry ", vol , " from table")
                response = dynamodb.delete_item(
                              TableName='cleanup',
                              Key={'volumeId':{'S':vol}}
                        )
                print(response)
                #go to the next volume id
                self.remove_entry_from_dynamodb(vol)
                continue
            delete = False
            for tag in tags:
               if tag['Key'] == 'Delete':
                  delete = tag.get('Value')
            
            #calculate date
            datetime_object = datetime.strptime(updated, '%m/%d/%y %H:%M:%S')
            today = datetime.today().strftime('%m/%d/%y %H:%M:%S')
            delay = abs(datetime.strptime(today, '%m/%d/%y %H:%M:%S') - datetime_object).days
            #check if the entry was made 1 day ago and delete tag is available. Remove volume after that.
            if delay >= 1 and delete:
                try:
                    response = ec2.delete_volume(
                        VolumeId = vol,
                        DryRun = False
                    )
                    print("volume ", vol," deleted by ebscleanup: on ", datetime.today().strftime('%m/%d/%y %H:%M:%S'))
                    listofdel.append(vol)
                    #remove the volumeid from dynamodb after deleting
                    self.remove_entry_from_dynamodb(vol)
                except Exception as e:
                    print("issue in deleting item from dynamodb ", e)
            #remove the entry from the table for donotdelete tagged ebs volumes
            for tag in tags:
               if tag['Key'] == 'DoNotDelete':
                  self.remove_entry_from_dynamodb(vol)
        return listofdel

    def update_delete_tag_of_ebs_volume(self,volumeId):
        today = datetime.today().strftime('%m/%d/%y %H:%M:%S')
        response = ec2.create_tags(
                                      DryRun=False,
                                      Resources=[
                                                  volumeId,
                                                ],
                                       Tags=[
                                               {
                                                'Key': 'Delete',
                                                'Value': 'True'
                                               },
                                               {
                                                'Key': 'TagUpdatedBy',
                                                'Value': 'Cleanup'
                                               },
                                               {
                                                   'Key': 'CleanupTagUpdatedOn',
                                                   'Value': today
                                               }
                                             ]
                                    )
        return response['ResponseMetadata']['HTTPStatusCode']

    def remove_delete_tag_of_ebs_volume(self,volumeId):
        response = ec2.delete_tags(
                                      DryRun=False,
                                      Resources=[
                                                  volumeId,
                                                ],
                                       Tags=[
                                               {
                                                'Key': 'Delete',
                                                'Value': 'True'
                                               },
                                               {
                                                'Key': 'TagUpdatedBy',
                                                'Value': 'Cleanup'
                                               },
                                             ]
                                    )
        return response['ResponseMetadata']['HTTPStatusCode']

    def remove_entry_from_dynamodb(self, vol):
        response = dynamodb.delete_item(
                              TableName='cleanup',
                              Key={'volumeId':{'S':vol}}
                        )
        print(response)

    def notify_list_of_unused_ebs(self, volume, deletedebs):
        subject = "Summary of Deleted EBS volumes and Status as available " + str (datetime.today().strftime('%m/%d/%y %H:%M:%S'))
        message = "The list of deleted volumes " + str(deletedebs) + ". And the list of available ebs volumes that are taged for deletion "+ str(volume)
        response = sns.publish(
                    TopicArn='arn:aws:sns:ap-south-1:123456789000:ebs',
                    Subject= subject,
                    Message= message
            )
        print(response)
#lambda handler
def lambda_handler(event, context):
    
    #scan the ebs volume which have status as available and skip the ones with user added dont delete tag
    clean = Cleanup()
    volumeId = clean.find_unused_ebs_volumes()
    for vol in volumeId:
        try:
            ebsTag = clean.update_delete_tag_of_ebs_volume(vol)
            update = clean.update_dynamodb(vol)
        except Exception as e:
            print("error in updating tag %s" % e)
    
    #validate the items from dynamodb
    response = clean.validate_dynamodb_entries()

    #send summary as email
    clean.notify_list_of_unused_ebs(volumeId,response)

    return {
        'statusCode': 200,
        'body': json.dumps('aws ebs cleanup completed')
    }
